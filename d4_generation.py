from timeit import default_timer as timer
from datetime import datetime
import time
import os
import sys
import warnings
import logging
import gc
import random

import numpy as np
from typing import Union
import tensorflow as tf
from tensorflow import keras

from d4_utils import (
    create_folder,
    log_file,
    hydrophobicity,
    h_bonding,
    charge,
    sasa,
    side_chain_length,
    aa_dict_pos,
    clear_log,
)
from d4_stats import validate, validation, pearson_spearman
from d4_split import split_inds, create_split_file
from d4_interactions import (
    atom_interaction_matrix_d,
    check_structure,
    model_interactions,
)
from d4_alignments import alignment_table
import d4_models as d4m

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
os.environ["PYTHONHASHSEES"] = str(0)
np.set_printoptions(threshold=sys.maxsize)


def augment(
    data: np.ndarray[tuple[int], np.dtype[str]],
    labels: np.ndarray[tuple[int], np.dtype[int | float]],
    mutations: np.ndarray[tuple[int], np.dtype[int]],
    runs: int = 3,
    un: bool = False,
) -> tuple[
    np.ndarray[tuple[int], np.dtype[str]],
    np.ndarray[tuple[int], np.dtype[int | float]],
    np.ndarray[tuple[int], np.dtype[int]],
]:
    """creates pseudo data from original data by adding it randomly
    :parameter
        - data:
          array of variants like ['S1A', 'D35T,V20R', ...]
        - labels:
          array with the corresponding scores of the provided data
        - mutations:
          array with number of mutations of each variant
        - runs:
          how often the augmentation should be performed
        - un:
          whether duplicated "new" variants should be removed
    :return
        - nd:
          augmented version of data
        - nl:
          augmented version of labels
        - nm:
          augmented version of mutations
    """

    # all possible indices of the data
    pos_inds = np.arange(len(labels))

    nd = []
    nl = []
    nm = []
    # do augmentation for #runs
    for i in range(runs):
        # random shuffle the inds that should be added
        np.random.shuffle(pos_inds)
        # add original labels and mutations with the original shuffled
        new_labels = labels + labels[pos_inds]
        new_mutations = mutations + mutations[pos_inds]

        new_data = []
        to_del = []
        # extract the mutations that are added and check if one contains the
        # same mutation and add this index to to_del
        # to later remove this augmentations
        for cj, (j, k) in enumerate(zip(data, data[pos_inds])):
            pos_new_data = np.sort(j.split(",") + k.split(","))
            # check the new data if it has the same mutation more than once
            # - if so add its index to the to_del(ete) ids
            if len(np.unique(pos_new_data)) != new_mutations[cj]:
                to_del.append(cj)
            new_data.append(",".join(pos_new_data))
        # remove the "wrong" augmentations
        new_labels = np.delete(new_labels, to_del)
        new_mutations = np.delete(new_mutations, to_del)
        new_data = np.asarray(new_data)
        new_data = np.delete(new_data, to_del)
        nd += new_data.tolist()
        nl += new_labels.tolist()
        nm += new_mutations.tolist()

    # remove duplicated entries
    if un:
        _, uni = np.unique(nd, return_index=True)
        nd = np.asarray(nd)[uni]
        nl = np.asarray(nl)[uni]
        nm = np.asarray(nm)[uni]

    return np.asarray(nd), np.asarray(nl), np.asarray(nm)


def data_generator_vals(
    wt_seq: str, alignment_path: str | None = None, alignment_base: str | None = None
) -> tuple[
    np.ndarray[tuple[int], np.dtype[int]],
    float,
    int,
    np.ndarray[tuple[int], np.dtype[int]],
    np.ndarray[tuple[int], np.dtype[float]],
    np.ndarray[tuple[int], np.dtype[int]],
    np.ndarray[tuple[int], np.dtype[int]],
    np.ndarray[tuple[int, int], np.dtype[int]],
    np.ndarray[tuple[int], np.dtype[float]],
    float,
    np.ndarray[tuple[int], np.dtype[int]],
    np.ndarray[tuple[int, 20], np.dtype[float]],
    np.ndarray[tuple[int], np.dtype[int]],
]:
    """returns values/ numpy arrays based on the wt_seq for the DataGenerator
    :parameter
        - wt_seq:
          wild type sequence as str eg 'AVLI'
        - alignment_path:
          path to the alignment file
        - alignment_base:
          name of the protein in the alignment file
    :returns
        - hm_pos_vals:
          values for interactions with valid hydrogen bonding partners
        - hp_norm:
          max value possible for hydrophobicity interactions
        - ia_norm:
          max value possible for interaction ares interactions
        - hm_converted:
          wt_seq converted into hydrogen bonding values
        - hp_converted:
          wt_seq converted into hydrophobicity values
        - cm_converted:
          wt_seq converted into charge values
        - ia_converted:
          wt_seq converted into SASA values
        - mat_index: 2D ndarray of float
          symmetrical index matrix
        - cl_converted:
          wt_seq converted into side chain length values
        - cl_norm:
          max value possible for two side chains
        - co_converted:
          wt_seq converted to amino acid positions in the alignment table
        - co_table:
          each row specifies which amino acids are conserved at that
          sequence position and how conserved they are
        - co_rows:
          inde help with indices of each sequence position"""

    hm_pos_vals = np.asarray([2, 3, 6, 9])

    h_vals = list(hydrophobicity.values())
    hp_norm = np.abs(max(h_vals) - min(h_vals))
    ia_norm = max(list(sasa.values())) * 2
    cl_norm = 2 * max(side_chain_length.values())

    hm_converted = np.asarray(list(map(h_bonding.get, wt_seq)))
    hp_converted = np.asarray(list(map(hydrophobicity.get, wt_seq)))
    cm_converted = np.asarray(list(map(charge.get, wt_seq)))
    ia_converted = np.asarray(list(map(sasa.get, wt_seq)))
    cl_converted = np.asarray(list(map(side_chain_length.get, wt_seq)))
    if alignment_path is not None:
        co_converted = np.asarray(list(map(aa_dict_pos.get, wt_seq)))
        co_table, co_rows = alignment_table(alignment_path, alignment_base)
    else:
        co_converted, co_table, co_rows = None, None, None

    wt_len = len(wt_seq)
    mat_size = wt_len * wt_len
    pre_mat_index = np.arange(mat_size).reshape(wt_len, wt_len) / (mat_size - 1)
    pre_mat_index = np.triu(pre_mat_index)
    mat_index = pre_mat_index + pre_mat_index.T - np.diag(np.diag(pre_mat_index))
    np.fill_diagonal(mat_index, 0)

    return (
        hm_pos_vals,
        hp_norm,
        ia_norm,
        hm_converted,
        hp_converted,
        cm_converted,
        ia_converted,
        mat_index,
        cl_converted,
        cl_norm,
        co_converted,
        co_table,
        co_rows,
    )


def progress_bar(num_batches: int, bar_len: int, batch: int) -> None:
    """prints progress bar with percentage that can be overwritten with a
    subsequent print statement - should be implemented with on_train_batch_end
    :parameter
        - num_batches:
          number of batches per epoch
        - bar_len:
          length of the progress bar
        - batch:
          number of the current batch
    :return
        None"""
    # current bar length - how many '=' the bar needs to have at current batch
    cur_bar = int(bar_len * (bar_len * (batch / bar_len) / num_batches))
    # cur_bar = int(bar_len * (batch / num_batches))

    # to get a complete bar at the end
    if batch == num_batches - 1:
        cur_bar = bar_len
    # printing the progress bar
    print(
        f"\r[{'=' * cur_bar}>{' ' * (bar_len - cur_bar)}] {(batch + 1) / num_batches * 100:0.0f}%",
        end="",
    )

    # set cursor to start of the line to overwrite progress bar when epoch
    # is done
    if num_batches - batch == 1:
        print(f"\r[{'=' * bar_len}>] 100%\r\r", end="")


class DataGenerator(keras.utils.Sequence):
    """
    Generates n_channel x n x n matrices to feed them as batches to a network
            where n denotes len(wild type sequence)
    modified after
    'https://stanford.edu/~shervine/blog/keras-how-to-generate-data-on-the-fly'
    ...
    Attributes:
    - features:
      features that should be encoded eg ['A2S,E3R' 'T6W']
    - labels:
      the corresponding labels to the features
    - interaction_matrix:
      boolean matrix whether residues interact or not
    - dim:
      dimensions of the matrices (len(wt_seq) x len(wt_seq))
    - n_channels:
      number of matrices used
    - batch_size:
      Batch size (if 1 gradient gets updated after every sample in training)
    - first_ind:
      index of the start of the protein sequence
    - hm_converted:
      wt sequence h-bonding encoded
    - hm_pos_vals:
      valid values for h-bonding residues
    - factor:
      1 - norm(distance) for all residues in the interaction matrix
    - hp_converted:
      wt sequence hydrophobicity encoded
    - hp_norm:
      max possible value for hydrophobicity change
    - cm_converted:
      wt sequence charge encoded
    - ia_converted:
      wt sequence interaction area encoded
    - ia_norm:
      max value for interaction area change
    - mat_index:
      symmetrical index matrix (for adjacency matrix) that represents the position of
      each interaction in the matrices
    - cl_converted:
      wild type sequence clash encoded
    - cl_norm:
      normalization value for the clash matrix
    - dist_mat:
      ture distances between all residues
    - dist_th
      maximum distance for residues to be counted as interaction
    - co_converted:
      wild type sequence position in alignment_table encoded
    - co_table:
      nx20 array- which amino acids are how conserved at which sequence
                 position
    - co_rows:
      indexing help for alignment_table
    - shuffle:
      if True data gets shuffled after every epoch
    - train:
      if True Generator returns features and labels (use during training)
      else only features
    """

    def __init__(
        self,
        features: np.ndarray[tuple[int], np.dtype[str]],
        labels: np.ndarray[tuple[int], np.dtype[int | float]],
        interaction_matrix: np.ndarray[tuple[int, int], np.dtype[bool]],
        dim: tuple[int, int],
        n_channels: int,
        batch_size: int,
        first_ind: int,
        hm_converted: np.ndarray[tuple[int], np.dtype[int]],
        hm_pos_vals: np.ndarray[tuple[int], np.dtype[int]],
        factor: np.ndarray[tuple[int, int], np.dtype[float]],
        hp_converted: np.ndarray[tuple[int], np.dtype[float]],
        hp_norm: float,
        cm_converted: np.ndarray[tuple[int], np.dtype[int]],
        ia_converted: np.ndarray[tuple[int], np.dtype[int]],
        ia_norm: int,
        mat_index: np.ndarray[tuple[int, int], np.dtype[int]],
        cl_converted: np.ndarray[tuple[int], np.dtype[float]],
        cl_norm: float,
        dist_mat: np.ndarray[tuple[int, int], np.dtype[float]],
        dist_th: int | float,
        co_converted: np.ndarray[tuple[int], np.dtype[int]],
        co_table: np.ndarray[tuple[int, 20], np.dtype[float]],
        co_rows: np.ndarray[tuple[int], np.dtype[int]],
        shuffle: bool = True,
        train: bool = True,
    ) -> None:
        self.features, self.labels = features, labels
        self.interaction_matrix = interaction_matrix
        self.dim = dim
        self.n_channels = n_channels
        self.batch_size = batch_size
        self.first_ind = first_ind
        self.hm_converted = hm_converted
        self.hm_pos_vals = hm_pos_vals
        self.factor = factor
        self.hp_converted = hp_converted
        self.hp_norm = hp_norm
        self.cm_converted = cm_converted
        self.ia_converted = ia_converted
        self.ia_norm = ia_norm
        self.mat_index = mat_index
        self.cl_converted = cl_converted
        self.cl_norm = cl_norm
        self.dist_mat = dist_mat
        self.dist_th = dist_th
        self.co_converted = co_converted
        self.co_table = co_table
        self.co_rows = co_rows
        self.shuffle = shuffle
        self.train = train

    def __len__(self):
        """number of batches per epoch"""
        return int(np.ceil(len(self.features) / self.batch_size))

    def __getitem__(self, idx: int):
        """Generate one batch of data"""
        features_batch = self.features[
            idx * self.batch_size : (idx + 1) * self.batch_size
        ]
        label_batch = self.labels[idx * self.batch_size : (idx + 1) * self.batch_size]

        f, l = self.__batch_variants(features_batch, label_batch)
        if self.train:
            return f, l
        else:
            return f

    def on_epoch_end(self):
        """Updates indexes after each epoch"""
        self.idx = np.arange(len(self.features))
        if self.shuffle:
            np.random.shuffle(self.idx)

    def __batch_variants(
        self,
        features_to_encode: np.ndarray[tuple[int], np.dtype[str]],
        corresponding_labels: np.ndarray[tuple[int], np.dtype[int | float]],
    ) -> np.ndarray[tuple[int, int, int], np.dtype[float]]:
        """creates interaction matrices of variants for a batch"""
        first_dim = corresponding_labels.shape[0]
        batch_features = np.empty((first_dim, *self.dim, self.n_channels))
        batch_labels = np.empty(first_dim, dtype=float)

        for ci, i in enumerate(features_to_encode):
            # variant i encoded as matrices
            final_matrix = model_interactions(
                feature_to_encode=i,
                interaction_matrix=self.interaction_matrix,
                index_matrix=self.mat_index,
                factor_matrix=self.factor,
                distance_matrix=self.dist_mat,
                dist_thrh=self.dist_th,
                first_ind=self.first_ind,
                hmc=self.hm_converted,
                hb=h_bonding,
                hm_pv=self.hm_pos_vals,
                hpc=self.hp_converted,
                hp=hydrophobicity,
                hpn=self.hp_norm,
                cmc=self.cm_converted,
                c=charge,
                iac=self.ia_converted,
                sa=sasa,
                ian=self.ia_norm,
                clc=self.cl_converted,
                scl=side_chain_length,
                cln=self.cl_norm,
                coc=self.co_converted,
                cp=aa_dict_pos,
                cot=self.co_table,
                cor=self.co_rows,
            )

            batch_features[ci] = final_matrix
            batch_labels[ci] = corresponding_labels[ci]
        return batch_features, batch_labels


class SaveToFile(keras.callbacks.Callback):
    """writes training stats in a temp file
     ...
    Attributes:
    - features: str
      path where the temp.csv file should be saved
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.start_time_epoch = time.time()

    def on_epoch_begin(self, epoch, logs=None):
        self.start_time_epoch = time.time()

    def on_epoch_end(self, epoch, logs=None):
        log_string = "{},{:0.4f},{:0.4f},{:0.4f},{}".format(
            str(epoch),
            logs["loss"],
            logs["val_loss"],
            time.time() - self.start_time_epoch,
            time.strftime("%H:%M:%S", time.localtime(self.start_time_epoch)),
        )
        with open(self.filepath, "a") as log_file_to_write:
            log_file_to_write.write(log_string + "\n")

    def on_train_end(self, logs=None):
        with open(self.filepath, "a") as log_file_to_write:
            log_file_to_write.write("Finished training")


class CustomPrint(keras.callbacks.Callback):
    """prints custom stats during training
    ...
    Attributes:
    - num_batches:
      number of batches per epoch
    - epoch_print:
      interval at which loss and the change in loss should be printed
    - epoch_stat_print:
      interval at which best train epoch, the best validation epoch and the
              difference in the loss between them
      should be printed
    - pb_len:
      length of the progress bar
    - model_d:
      filepath where the models should be saved
    - model_save_interval:
      minimum number of epochs to pass to save the model - only gets saved
      when the validation loss has improved
      since the last time the model was saved
    - save:
      whether to save the model
    """

    def __init__(
        self,
        num_batches: int,
        epoch_print: int = 1,
        epoch_stat_print: int = 10,
        pb_len: int = 60,
        model_d: str = "",
        model_save_interval: int = 5,
        save: bool = False,
    ) -> None:
        self.epoch_print = epoch_print
        self.best_loss = np.Inf
        self.bl_epoch = 0
        self.best_val_loss = np.Inf
        self.bvl_epoch = 0
        self.latest_loss = 0.0
        self.latest_val_loss = 0.0
        self.epoch_stat_print = epoch_stat_print
        self.start_time_epoch = 0.0
        self.start_time_training = 0.0
        self.num_batches = num_batches
        self.pb_len = pb_len
        self.model_d = model_d
        self.epoch_since_model_save = 0
        self.model_save_interval = model_save_interval
        self.save = save

    def on_train_begin(self, logs=None):
        self.start_time_training = time.time()

    def on_epoch_begin(self, epoch, logs=None):
        self.start_time_epoch = time.time()
        if epoch == 0:
            print("*** training started ***")

    def on_train_batch_end(self, batch, logs=None):
        progress_bar(num_batches=self.num_batches, bar_len=self.pb_len, batch=batch)

    def on_epoch_end(self, epoch, logs=None):
        # loss and validation loss of this epoch
        cur_loss = logs["loss"]
        cur_val_loss = logs["val_loss"]

        if epoch % self.epoch_print == 0:
            print(
                f"E {epoch:<3} - loss: {cur_loss: 0.4f}  val_loss: {cur_val_loss: 0.4f}",
                f"- loss change: {cur_loss - self.latest_loss: 0.4f}  ",
                f"val_loss change: {cur_val_loss - self.latest_val_loss: 0.4f} - ",
                f"seconds per epoch: {time.time() - self.start_time_epoch: 0.4f}\n",
                end="",
            )
        # update the latest loss and latest validation loss to loss of this epoch
        self.latest_loss = cur_loss
        self.latest_val_loss = cur_val_loss
        # update the best loss if loss of current epoch was better
        if cur_loss < self.best_loss:
            self.best_loss = cur_loss
            self.bl_epoch = epoch
        # update the best validation loss if current epoch was better
        if cur_val_loss < self.best_val_loss:
            self.best_val_loss = cur_val_loss
            self.bvl_epoch = epoch
            # save model if the validation loss improved since the last time i
            # it was saved and min model_save_interval epochs have passed
            if self.save:
                if epoch - self.epoch_since_model_save >= self.model_save_interval:
                    self.model.save(self.model_d, overwrite=True)
                    self.epoch_since_model_save = epoch
        # print stats of the epoch after the given epoch_stat_print interval
        if epoch % self.epoch_stat_print == 0 and epoch > 0:
            d = np.abs(self.best_loss - self.best_val_loss)
            if d != 0.0 and self.best_val_loss != 0.0:
                dp = (d / self.best_val_loss) * 100
            else:
                dp = np.nan
            d_cl = cur_loss - self.best_loss
            d_cvl = cur_val_loss - self.best_val_loss

            print(
                f"Best train epoch: {self.bl_epoch}\n",
                f"\rBest validation epoch: {self.bvl_epoch}\n",
                f"\rdelta: {d:0.4f} (equals {dp:0.2f}% of val_loss)\n",
                f"\rdifference to best loss ({self.best_loss:0.4f}): {d_cl:0.4f}\n",
                f"\rdifference to best val_loss ({self.best_val_loss:0.4f}): "
                f"{d_cvl:0.4f}\n",
            )

    def on_train_end(self, logs=None):
        # save model in the end and print overall training stats
        if self.save:
            self.model.save(self.model_d + "_end")
        print()
        print(
            "Overall best epoch stats\n",
            "\rBest training epoch: "
            f"{self.bl_epoch} with a loss of {self.best_loss:0.4f}",
        )
        print(
            f"Best validation epoch: {self.bvl_epoch} with a loss of "
            f"{self.best_val_loss:0.4f}"
        )
        print(
            "Total training time in minutes: "
            f"{(time.time() - self.start_time_training) / 60:0.1f}\n"
        )


class ClearMemory(keras.callbacks.Callback):
    """clears garbage collection and clears session after each epoch
    ...
    Attributes:
    None
    """

    def on_epoch_end(self, epoch, logs=None):
        gc.collect()
        tf.keras.backend.clear_session()


def run_all(
    model_to_use: str,
    optimizer: str,
    tsv_file: str,
    pdb_file: str,
    wt_seq: str,
    number_mutations: str,
    variants: str,
    score: str,
    dist_thr: int | float,
    max_train_mutations: int | None,
    training_epochs: int,
    test_num: int,
    first_ind: int,
    algn_path: str | None = None,
    algn_bl: str | None = None,
    r_seed: int | None = None,
    deploy_early_stop: bool = True,
    es_monitor: str = "val_loss",
    es_min_d: int | float = 0.01,
    es_patience: int = 20,
    es_mode: str = "auto",
    es_restore_bw: bool = True,
    load_trained_model: str | None = None,
    batch_size: int = 64,
    save_fig: str | None = None,
    show_fig: bool = False,
    write_to_log: bool = True,
    silent: bool = False,
    extensive_test: bool = False,
    save_model: bool = False,
    load_trained_weights: str | None = None,
    no_nan: bool = True,
    settings_test: bool = False,
    p_dir: str = "",
    split_def: Union[list[int | float], None] = None,
    validate_training: bool = False,
    lr: float = 0.001,
    transfer_conv_weights: str | None = None,
    train_conv_layers: bool = False,
    write_temp: bool = False,
    split_file_creation: bool = False,
    use_split_file: str | None = None,
    daug: bool = False,
    clear_el: bool = False,
    reduce: bool = False,
    jit: bool = True,
):
    """runs all functions to train a neural network
    :parameter
    - model_to_use:
      function that returns the model
    - optimizer:
      keras optimizer to be used
    - tsv_file:
      path to tsv file containing dms data of the protein of interest
    - pdb_file:
      path to pdb file containing the structure
    - wt_seq:
      wt sequence of the protein of interest eg. 'AVL...'
    - number_mutations:
      how the number of mutations column is named
    - variants:
      name of the variant column
    - score:
      name of the score column
    - dist_thr:
      threshold distances between any side chain atom to count as interacting
    - max_train_mutations:
      - int specifying maximum number of mutations per sequence to be used for training
      - None to use all mutations for training
    - training_epochs:
      number of epochs used for training the model
    - test_num:
      number of samples for the test after the model was trained
    - first_ind:
      offset of the start of the sequence (when sequence doesn't start with residue 0)
    - algn_path:
      path to the multiple sequence alignment in clustalw format
    - algn_bl:
      name of the wild type sequence in the alignment file
    - r_seed:
      numpy and tensorflow random seed
    - deploy_early_stop:
      whether early stop during training should be enabled (True) or not (False)
            - es_monitor:
              what to monitor to determine whether to stop the training or not
            - es_min_d:
              min_delta - min difference in es_monitor to not stop training
            - es_patience:
              number of epochs the model can try to decrease its es_monitor value for
              at least min_delta before stopping
            - es_mode:
              direction of quantity monitored in es_monitor
            - es_restore_bw:
              True stores the best weights of the training - False stores the last
    - batch_size:
      after how many samples the gradient gets updated
    - load_trained_model:
      path to an already trained model or None to not load a model
    - save_fig:
            - None to not save figures
            - str specifying the file path where the figures should be stored
    - show_fig:
      True to show figures
    - write_to_log:
      if True writes all parameters used in the log file - **should be always enabled**
    - silent:
      True to print stats in the terminal
    - extensive_test:
      if True more test are done and more detailed plots are created
    - save_model:
      True to save the model after training
    - load_trained_weights:
      path to model of who's weights should be used None if it shouldn't be used
    - no_nan:
      True terminates training on nan
    - settings_test:
      True doesn't train the model and only executes everything of the function
      that is before model.fit()
    - p_dir:
      path to the projects content root
    - split_def:
      specifies the split for train, tune, test indices
            - float specifies fractions of the whole dataset
              eg [0.25, 0.25, 0.5] creates a train and tune dataset with 50 entries
              each and a test dataset of 100
              if the whole dataset contains 200 entries
            - int specifies the different number of samples per dataset
              eg [50,50,100] leads to a train and a tune dataset with 50 entries
              each and a test dataset of 100
              if the whole dataset contains 200 entries
            - None uses [0.8, 0.15, 0.05] as split
    - validate_training:
      if True validation of the training will be performed
    - lr:
      learning rate (how much the weights can change during an update)
    - transfer_conv_weights:
      path to model who's weights of it's convolution layers should be used for
      transfer learning - needs to have the same architecture for the convolution part
      as the newly build model (model_to_use) or None to not transfer weights
    - train_conv_layers:
      if True convolution layers are trainable - only applies when
      transfer_conv_weights is not None
    - write_temp:
      if True writes mae, loss and time per epoch of each epoch to the temp.csv in
      result_files
    - split_file_creation:
      if True creates a directory containing train.txt, tune.txt and test.txt files
      that store the indices of the rows used from the tsv file during
      training, validating and testing
    - use_split_file:
      if not None this needs the file_path to a directory containing splits specifying
      the 'train', 'tune', 'test' indices - these files need to be named
      'train.txt', 'tune.txt' and 'test.txt'
      otherwise splits of the tsv file according to split_def will be used
    - daug:
      True to use data augmentation
    - clear_el:
      if True error log gets cleared before a run
    - reduce:
      if True a size reducing intro layer is used
    - jit:
      it True jit_compile from tensorflow is used
    :return
        None
    """
    try:
        # dictionary with argument names as keys and the input as values
        arg_dict = locals()

        # convert inputs to their respective function
        model_to_use = getattr(d4m, model_to_use)
        architecture_name = model_to_use.__code__.co_name
        optimizer = getattr(tf.keras.optimizers, optimizer)

        # getting the proteins name
        p_name = os.path.split(tsv_file)[1].split(".")[0]

        # creating a "unique" name for protein
        time_ = str(datetime.now().strftime("%d_%m_%Y_%H%M%S")).split(" ")[0]
        name = "{}_{}".format(p_name, time_)
        print(name)

        # path of the directory where results are stored
        result_dir = os.path.join(p_dir, "result_files")
        # path where the temp_file is located
        temp_path = os.path.join(result_dir, "temp.csv")
        # path where the log_file is located
        log_file_path = os.path.join(result_dir, "log_file.csv")
        # error log file path
        error_log_path = os.path.join(result_dir, "error.log")
        # dir where models are stored
        model_base_dir = os.path.join(result_dir, "saved_models")
        recent_model_dir = os.path.join(result_dir, "saved_models", name)

        # create result dir, base model dir and recent model dir if they don't exist
        if not os.path.isdir(result_dir):
            os.mkdir(result_dir)
        if save_model:
            if not os.path.isdir(model_base_dir):
                os.mkdir(model_base_dir)
            if not os.path.isdir(recent_model_dir):
                os.mkdir(recent_model_dir)

        # clear temp file from previous content or create it if it doesn't exist
        clear_log(
            temp_path,
            name + "\n" + "epoch,loss,val_loss,time_in_sec,epoch_start_time\n",
        )

        # clear error.log from previous run or create it if it doesn't exist
        if not os.path.exists(error_log_path) or clear_el:
            clear_log(error_log_path)

        # resets all state generated by keras
        tf.keras.backend.clear_session()

        # check for write_to_log
        if not write_to_log:
            warnings.warn(
                "Write to log file disabled - not recommend behavior", UserWarning
            )

        # set random seed
        if r_seed is not None:
            np.random.seed(r_seed)
            tf.random.set_seed(r_seed)
            random.seed(r_seed)

        # creates a directory where plots will be saved
        if (save_fig and validate_training) or (save_fig and extensive_test):
            save_fig = os.path.join(result_dir, "plots_" + name)
            if not os.path.isdir(save_fig):
                os.mkdir(save_fig)
        else:
            save_fig = None

        if not settings_test:
            # writes used arguments to log file
            if write_to_log:
                header = (
                    "name," + ",".join(list(arg_dict.keys())) + ",training_time_in_min"
                )
                prep_values = []
                for i in list(arg_dict.values()):
                    if type(i) == list:
                        try:
                            prep_values.append("".join(i))
                        except TypeError:
                            prep_values.append(
                                "".join(str(i)).replace(",", "_").replace(" ", "")
                            )
                    else:
                        prep_values.append(str(i))
                values = name + "," + ",".join(prep_values) + ",nan"
                log_file(log_file_path, values, header)

        starting_time = timer()

        # creating a list of the wt sequence string e.g. 'AVL...' -> ['A', 'V', 'L',...]
        wt_seq = list(wt_seq)

        # split dataset
        ind_dict, data_dict = split_inds(
            file_path=tsv_file,
            variants=variants,
            score=score,
            number_mutations=number_mutations,
            split=split_def,
            split_file_path=use_split_file,
            test_name="stest",
        )

        # Create files with the corresponding indices of the train, tune and test splits
        if split_file_creation:
            create_split_file(
                p_dir=result_dir,
                name=name,
                train_split=ind_dict["train"],
                tune_split=ind_dict["tune"],
                test_split=ind_dict["test"],
            )

        # data to train the model on
        # variants
        train_data = data_dict["train_data"]
        # their respective scores
        train_labels = data_dict["train_labels"]
        # number of mutations per variant
        train_mutations = data_dict["train_mutations"]

        # restrict training data to certain number of mutations per variant
        if max_train_mutations is not None:
            mtm_bool = train_mutations <= max_train_mutations
            train_data = train_data[mtm_bool]
            train_labels = train_labels[mtm_bool]
            train_mutations = train_mutations[mtm_bool]

        if daug:
            # original data
            otd = data_dict["train_data"]
            otl = data_dict["train_labels"]
            otm = data_dict["train_mutations"]
            ot_len = len(otl)

            # data augmentation
            decay = 0.2
            cap = 20000
            for i in range(3):
                aug_data, aug_labels, aug_mutations = augment(
                    train_data, train_labels, train_mutations, runs=4
                )
                # concatenation of original and augmented train data
                train_data = np.concatenate((train_data, aug_data))
                train_labels = np.concatenate(
                    (train_labels, aug_labels * (1 - i * decay))
                )
                train_mutations = np.concatenate((train_mutations, aug_mutations))
            nt_len = len(train_labels)

            # shuffle augmented data
            s_inds = np.arange(nt_len)
            # np.random.shuffle(s_inds)
            train_data = train_data[s_inds]
            train_labels = train_labels[s_inds]
            train_mutations = train_mutations[s_inds]
            # only use as much fake data as needed to get cap# of training data or all
            # if not enough could be created
            if nt_len + ot_len > cap:
                # number of augmented data needed to get cap# of training data points
                need = cap - ot_len
                print("{} augmented data points created".format(str(len(train_data))))
                if need < 0:
                    need = 0
                print(
                    "{} of them and {} original data points used in training".format(
                        str(need), str(ot_len)
                    )
                )
                if need > 0:
                    train_data = np.concatenate((train_data[:need], otd))
                    train_labels = np.concatenate((train_labels[:need], otl))
                    train_mutations = np.concatenate((train_mutations[:need], otm))
                # if enough original data is available
                else:
                    train_data = otd
                    train_labels = otl
                    train_mutations = otm
            # use all the augmented data if it + original data is less than cap#
            else:
                train_data = np.concatenate((train_data, otd))
                train_labels = np.concatenate((train_labels, otl))
                train_mutations = np.concatenate((train_mutations, otm))

            print("new train split size:", len(train_data))

        # ---
        "test data restriction"
        tdr = int(len(data_dict["train_data"]) * 0.2)
        # !!! REMOVE the slicing for test_data !!!

        # data to validate during training
        test_data = data_dict["tune_data"][:tdr]
        test_labels = data_dict["tune_labels"][:tdr]
        test_mutations = data_dict["tune_mutations"][:tdr]

        # data the model has never seen
        unseen_data = data_dict["test_data"]
        unseen_labels = data_dict["test_labels"]
        unseen_mutations = data_dict["test_mutations"]

        # create test data for the test_generator
        if len(unseen_mutations) > 0:
            if test_num > len(unseen_data):
                test_num = len(unseen_data)
            pos_test_inds = np.arange(len(unseen_data))
            test_inds = np.random.choice(pos_test_inds, size=test_num, replace=False)
            t_data = unseen_data[test_inds]
            t_labels = unseen_labels[test_inds]
            t_mutations = unseen_mutations[test_inds]
            print(
                "\n--- will be using unseen data for final model performance"
                " evaluation ---\n"
            )
        else:
            if test_num > len(test_data):
                test_num = len(test_data)
            pos_test_inds = np.arange(len(test_data))
            test_inds = np.random.choice(pos_test_inds, size=test_num, replace=False)
            t_data = test_data[test_inds]
            t_labels = test_labels[test_inds]
            t_mutations = test_mutations[test_inds]
            print(
                "\n--- will be using validation data for evaluating the models"
                " performance ---\n"
            )

        # possible values and encoded wt_seq (based on different properties) for the
        # DataGenerator
        (
            hm_pos_vals,
            hp_norm,
            ia_norm,
            hm_converted,
            hp_converted,
            cm_converted,
            ia_converted,
            mat_index,
            cl_converted,
            cl_norm,
            co_converted,
            co_table,
            co_rows,
        ) = data_generator_vals(wt_seq, algn_path, algn_bl)

        # distance-, factor- and interaction matrix
        dist_m, factor, comb_bool = atom_interaction_matrix_d(
            pdb_file, dist_th=dist_thr, plot_matrices=show_fig
        )

        # checks whether sequence in the pdb and the wt_seq match
        check_structure(pdb_file, comb_bool, wt_seq)

        # number of matrices used to encode structure
        channel_num = 7
        if algn_path is None:
            channel_num = 6

        # neural network model function
        model = model_to_use(wt_seq, channel_num, reduce=reduce)

        # load weights to model
        if load_trained_weights is not None:
            old_model = keras.models.load_model(load_trained_weights)
            model.set_weights(old_model.get_weights())

        # loads a model defined in load_trained_model and ignores the model built above
        if load_trained_model is not None:
            model = keras.models.load_model(load_trained_model)

        # load weights of a models convolutional part to a model that has a
        # convolution part with the same architecture
        # but maybe a different/ not trained classifier
        if transfer_conv_weights is not None:
            # loads model and its weights
            trained_model = keras.models.load_model(transfer_conv_weights)
            temp_weights = [layer.get_weights() for layer in trained_model.layers]

            # which layers are conv layers (or not dense or flatten since these are
            # sensitive to different input size)
            transfer_layers = []
            for i in range(len(trained_model.layers)):
                if i > 0:
                    l_name = trained_model.layers[i].name
                    # select all layers apart from Dense and Flatten
                    layer_i = trained_model.layers[i]
                    if not any(
                        [
                            isinstance(layer_i, keras.layers.Dense),
                            isinstance(layer_i, keras.layers.Flatten),
                        ]
                    ):
                        transfer_layers.append(i)

            # Transfer weights to new model
            # fraction of layers that should be transferred (1. all conv layer weighs
            # get transferred)
            fraction_to_train = 1.0  # 0.6
            for i in transfer_layers[: int(len(transfer_layers) * fraction_to_train)]:
                model.layers[i].set_weights(temp_weights[i])
                if train_conv_layers is False:
                    model.layers[i].trainable = False

            # summary of the new model
            model.summary()

        model.compile(
            optimizer(learning_rate=lr),
            loss="mean_absolute_error",
            metrics=["mae"],
            jit_compile=jit,
        )

        all_callbacks = []
        # deploying early stop parameters
        if deploy_early_stop:
            es_callback = tf.keras.callbacks.EarlyStopping(
                monitor=es_monitor,
                min_delta=es_min_d,
                patience=es_patience,
                mode=es_mode,
                restore_best_weights=es_restore_bw,
            )
            all_callbacks.append(es_callback)

        # stops training on nan
        if no_nan:
            all_callbacks.append(tf.keras.callbacks.TerminateOnNaN())

        # save mae and loss to temp file
        if write_temp:
            all_callbacks.append(SaveToFile(temp_path))

        # clear Session after each epoch
        # all_callbacks.append(ClearMemory())

        # custom stats print
        # number of batches needed for status bar increments
        n_batches = int(np.ceil(len(train_data) / batch_size))
        all_callbacks.append(
            CustomPrint(
                num_batches=n_batches,
                epoch_print=1,
                epoch_stat_print=10,
                model_d=recent_model_dir,
                save=save_model,
            )
        )

        # parameters for the DataGenerator
        params = {
            "interaction_matrix": comb_bool,
            "dim": comb_bool.shape,
            "n_channels": channel_num,
            "batch_size": batch_size,
            "first_ind": first_ind,
            "hm_converted": hm_converted,
            "hm_pos_vals": hm_pos_vals,
            "factor": factor,
            "hp_converted": hp_converted,
            "hp_norm": hp_norm,
            "cm_converted": cm_converted,
            "ia_converted": ia_converted,
            "ia_norm": ia_norm,
            "mat_index": mat_index,
            "cl_converted": cl_converted,
            "cl_norm": cl_norm,
            "dist_mat": dist_m,
            "dist_th": dist_thr,
            "co_converted": co_converted,
            "co_table": co_table,
            "co_rows": co_rows,
            "shuffle": True,
            "train": True,
        }

        test_params = {
            "interaction_matrix": comb_bool,
            "dim": comb_bool.shape,
            "n_channels": channel_num,
            "batch_size": batch_size,
            "first_ind": first_ind,
            "hm_converted": hm_converted,
            "hm_pos_vals": hm_pos_vals,
            "factor": factor,
            "hp_converted": hp_converted,
            "hp_norm": hp_norm,
            "cm_converted": cm_converted,
            "ia_converted": ia_converted,
            "ia_norm": ia_norm,
            "mat_index": mat_index,
            "cl_converted": cl_converted,
            "cl_norm": cl_norm,
            "dist_mat": dist_m,
            "dist_th": dist_thr,
            "co_converted": co_converted,
            "co_table": co_table,
            "co_rows": co_rows,
            "shuffle": False,
            "train": False,
        }

        # DataGenerator for training and the validation during training
        training_generator = DataGenerator(train_data, train_labels, **params)
        validation_generator = DataGenerator(test_data, test_labels, **params)
        test_generator = DataGenerator(t_data, np.zeros(len(t_labels)), **test_params)

        if not settings_test:
            # training
            history = model.fit(
                training_generator,
                validation_data=validation_generator,
                epochs=training_epochs,
                use_multiprocessing=True,
                workers=12,
                callbacks=[all_callbacks],
                verbose=0,
            )

            end_time = timer()

            # adds training time to result_files and replaces the nan time
            if write_to_log:
                log_f = open(log_file_path, "r")
                prev_log = log_f.readlines()
                log_f.close()
                log_cont_len = len(prev_log)
                w_log = open(log_file_path, "w+")
                for ci, i in enumerate(prev_log):
                    if len(prev_log) > 1:
                        if log_cont_len - ci == 1:
                            loi = i.strip().split(",")
                            loi[-1] = str(np.round((end_time - starting_time) / 60, 0))
                            w_log.write(",".join(loi) + "\n")
                        else:
                            w_log.write(i)
                w_log.close()

            # training and validation plot of the training
            if validate_training:
                try:
                    validate(
                        validation_generator,
                        model,
                        history,
                        name,
                        save_fig_v=save_fig,
                        plot_fig=show_fig,
                    )
                except ValueError:
                    print("Plotting validation failed due to nan in training")

            # calculating pearsons' r and spearman r for the test dataset
            mae, mse, pearsonr, pp, spearmanr, sp = pearson_spearman(
                model, test_generator, t_labels
            )
            print(
                "{:<12s}{:0.4f}\n{:<12s}{:0.4f}\n{:<12s}{:0.4f}\n{:<12s}{:0.4f}"
                "\n{:<12s}{:0.4f}\n{:<12s}{:0.4f}\n".format(
                    "MAE",
                    mae,
                    "MSE",
                    mse,
                    "PearsonR",
                    pearsonr,
                    "PearsonP",
                    pp,
                    "SpearmanR",
                    spearmanr,
                    "SpearmanP",
                    sp,
                )
            )

            # creating more detailed plots
            if extensive_test:
                validation(
                    model=model,
                    generator=test_generator,
                    labels=t_labels,
                    v_mutations=t_mutations,
                    p_name=p_name,
                    test_num=test_num,
                    save_fig=save_fig,
                    plot_fig=show_fig,
                    silent=silent,
                )

            # data for the result file
            result_string = ",".join(
                [
                    name,
                    architecture_name,
                    str(len(train_data)),
                    str(len(test_data)),
                    str(np.round(mae, 4)),
                    str(np.round(mse, 4)),
                    str(np.round(pearsonr, 4)),
                    str(np.round(pp, 4)),
                    str(np.round(spearmanr, 4)),
                    str(np.round(sp, 4)),
                ]
            )
            if write_to_log:
                # writing results to the result file
                log_file(
                    os.path.join(result_dir, "results.csv"),
                    result_string,
                    "name,architecture,train_data_size,test_data_size,mae,mse,"
                    "pearson_r,pearson_p,spearman_r,spearman_p",
                )

        gc.collect()
        del model

    except Exception as e:
        # writing exception to error.log
        result_dir = os.path.join(p_dir, "result_files")
        if not os.path.exists(result_dir):
            os.mkdir(result_dir)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(message)s",
            datefmt="%d/%m/%Y %I:%M:%S %p",
            handlers=[
                logging.FileHandler(os.path.join(result_dir, "error.log")),
                logging.StreamHandler(sys.stdout),
            ],
        )
        logging.exception(e)


if __name__ == "__main__":
    pass
