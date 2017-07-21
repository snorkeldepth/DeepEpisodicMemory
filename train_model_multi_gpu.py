import tensorflow as tf
import math, os, time, json
import data_prep.model_input as input
import data_postp.similarity_computations as similarity_computations
from pprint import pprint
from data_prep.TFRW2Images import createGif

from utils.helpers import get_iter_from_pretrained_model, learning_rate_decay, remove_items_from_dict
from utils.io_handler import create_session_dir, create_subfolder, store_output_frames_as_gif, write_metainfo, store_latent_vectors_as_df, store_encoder_latent_vector, file_paths_from_directory

from tensorflow.python.platform import app
from tensorflow.python.platform import flags
from models import loss_functions
import numpy as np

""" Set Model From Model Zoo"""
from models.model_zoo import model_conv5_fc_lstm2_1000_deep_64 as model
""""""



# I/O constants
FLAGS = flags.FLAGS
OUT_DIR = '/localhome/rothfuss/training'
#DATA_PATH = '/PDFData/rothfuss/data/activity_net/tf_records_pc031'
DATA_PATH = '/PDFData/rothfuss/data/20bn-something/tf_records_train'
#OUT_DIR = '/home/ubuntu/training'
#DATA_PATH = '/PDFData/rothfuss/data/UCF101/tf_record'

# other constants
LOSS_FUNCTIONS = ['mse', 'gdl', 'mse_gdl']
IMAGE_RANGE_START = 5 # parameter that controls the index of the starting image for the train/valid batch
NUMBER_OVERALL_IMAGES = 20

# for pretraining-mode only
PRETRAINED_MODEL = '/common/homes/students/rothfuss/Documents/training/07-15-17_15-09'
# use pre-trained model and run validation only
VALID_ONLY = False
VALID_MODE = 'data_frame' # 'vector', 'gif', 'similarity', 'data_frame'
EXCLUDE_FROM_RESTORING = None


# model hyperparameters
flags.DEFINE_integer('num_iterations', 1000000, 'specify number of training iterations, defaults to 100000')
flags.DEFINE_string('loss_function', 'mse', 'specify loss function to minimize, defaults to gdl')
flags.DEFINE_string('batch_size', 50, 'specify the batch size, defaults to 50')
flags.DEFINE_integer('valid_batch_size', 128, 'specify the validation batch size, defaults to 50')
flags.DEFINE_bool('uniform_init', False, 'specifies if the weights should be drawn from gaussian(false) or uniform(true) distribution')
flags.DEFINE_integer('num_gpus', 1, 'specifies the number of available GPUs of the machine')

flags.DEFINE_string('encoder_length', 5, 'specifies how many images the encoder receives, defaults to 5')
flags.DEFINE_string('decoder_future_length', 5, 'specifies how many images the future prediction decoder receives, defaults to 5')
flags.DEFINE_string('decoder_reconst_length', 5, 'specifies how many images the reconstruction decoder receives, defaults to 5')
flags.DEFINE_bool('fc_layer', True, 'indicates whether fully connected layer shall be added between encoder and decoder')
flags.DEFINE_float('learning_rate_decay', 0.000008, 'learning rate decay factor')
flags.DEFINE_integer('learning_rate', 0.0001, 'initial learning rate for Adam optimizer')
flags.DEFINE_float('noise_std', 0.1, 'defines standard deviation of gaussian noise to be added to the hidden representation during training')
flags.DEFINE_float('keep_prob_dopout', 0.85, 'keep probability for dropout during training, for valid automatically 1')

#IO flags specifications
flags.DEFINE_string('path', DATA_PATH, 'specify the path to where tfrecords are stored, defaults to "../data/"')
flags.DEFINE_integer('num_channels', 3, 'number of channels in the input frames')
flags.DEFINE_string('output_dir', OUT_DIR, 'directory for model checkpoints.')
flags.DEFINE_string('pretrained_model', PRETRAINED_MODEL, 'filepath of a pretrained model to initialize from.')
flags.DEFINE_string('valid_only', VALID_ONLY, 'Set to "True" if you want to validate a pretrained model only (no training involved). Defaults to False.')
flags.DEFINE_string('valid_mode', VALID_MODE, 'When set to '
                                              '"vector": encoder latent vector for each validation is exported to "output_dir" (only when VALID_ONLY=True) '
                                              '"gif": gifs are generated from the videos'
                                              '"similarity": compute (cos) similarity matrix')
flags.DEFINE_string('exclude_from_restoring', EXCLUDE_FROM_RESTORING, 'variable names to exclude from saving and restoring')

# intervals
flags.DEFINE_integer('valid_interval', 200, 'number of training steps between each validation')
flags.DEFINE_integer('summary_interval', 100, 'number of training steps between summary is stored')
flags.DEFINE_integer('save_interval', 2000, 'number of training steps between session/model dumps')


class Model:

  def __init__(self, summary_prefix, reuse_scope=None):

    self.learning_rate = tf.placeholder_with_default(FLAGS.learning_rate, ())
    self.iter_num = tf.placeholder_with_default(FLAGS.num_iterations, ())
    self.summaries = []
    self.noise_std = tf.placeholder_with_default(FLAGS.noise_std, ())
    self.opt = tf.train.AdamOptimizer(self.learning_rate)


    assert IMAGE_RANGE_START + FLAGS.encoder_length + FLAGS.decoder_future_length <= NUMBER_OVERALL_IMAGES and IMAGE_RANGE_START >= 0, \
            "settings for encoder/decoder lengths along with starting range exceed number of available images"
    assert FLAGS.encoder_length >= FLAGS.decoder_reconst_length, "encoder must be at least as long as reconstructer"


    if reuse_scope is None:  # train model
      tower_grads = []
      tower_losses = []
      for i in range(FLAGS.num_gpus):
        train_batch, _, _ = input.create_batch(FLAGS.path, 'train', FLAGS.batch_size,
                                               int(math.ceil(
                                                 FLAGS.num_iterations / (FLAGS.batch_size * 20))),
                                               False)
        train_batch = tf.cast(train_batch, tf.float32)
        with tf.device('/gpu:%d' % i):
          with tf.name_scope('%s_%d' % ('tower', i)):
            tower_loss, _, _, _ = tower_operations(train_batch[:,IMAGE_RANGE_START:,:,:,:], train=True)
            tower_losses.append(tower_loss)

            # Reuse variables for the next tower.
            tf.get_variable_scope().reuse_variables()

            grads = self.opt.compute_gradients(tower_loss)
            tower_grads.append(grads)

      with tf.device('/cpu:0'):
        #copmute average loss
        self.loss = average_losses(tower_losses)

        #compute average over gradients of all towers
        grads = average_gradients(tower_grads)

        # Apply the gradients to adjust the shared variables.
        self.train_op= self.opt.apply_gradients(grads)

      #measure batch time
      self.elapsed_time = tf.placeholder(tf.float32, [])
      self.summaries.append(tf.summary.scalar('batch_duration', self.elapsed_time))

    else: # validation model
      with tf.variable_scope(reuse_scope, reuse=True):
        tower_losses, frames_pred_list, frames_reconst_list, hidden_repr_list, label_batch_list, \
        metadata_batch_list, val_batch_list = [], [], [], [], [], [], []

        for i in range(FLAGS.num_gpus):
          val_batch, label_batch, metadata_batch = input.create_batch(FLAGS.path, 'valid', FLAGS.valid_batch_size,
                                               int(math.ceil(
                                                 FLAGS.num_iterations / (FLAGS.batch_size * 20))),
                                               False)

          val_batch = tf.cast(val_batch, tf.float32)
          self.val_batch = val_batch

          with tf.device('/gpu:%d' % i):
            with tf.name_scope('%s_%d' % ('tower', i)):
              tower_loss, frames_pred, frames_reconst, hidden_repr = tower_operations(val_batch[:,IMAGE_RANGE_START:,:,:,:], train=False)
              tower_losses.append(tower_loss)
              frames_pred_list.append(tf.pack(frames_pred))
              frames_reconst_list.append(tf.pack(frames_reconst))
              hidden_repr_list.append(hidden_repr)

              val_batch_list.append(val_batch)
              label_batch_list.append(label_batch)
              metadata_batch_list.append(metadata_batch)
              # Reuse variables for the next tower.
              tf.get_variable_scope().reuse_variables()

        with tf.device('/cpu:0'):
          # compute average loss
          self.loss = average_losses(tower_losses)
          # concatenate outputs of towers to one large tensor each
          self.frames_pred = tf.unstack(tf.concat(1, frames_pred_list))
          self.frames_reconst = tf.unstack(tf.concat(1, frames_reconst_list))
          self.hidden_repr = tf.concat(0, hidden_repr_list)
          self.label = tf.concat(0, label_batch_list)
          self.metadata = tf.concat(0, metadata_batch_list)
          val_set = tf.concat(0, val_batch_list)

      self.add_image_summary(summary_prefix, val_set, FLAGS.encoder_length, FLAGS.decoder_future_length,
                           FLAGS.decoder_reconst_length)

    if reuse_scope and FLAGS.valid_only: # only valid mode - evaluate frame predictions for storing on disk
      self.output_frames = self.frames_reconst + self.frames_pred #join arrays of tensors

    self.summaries.append(tf.summary.scalar(summary_prefix + '_loss', self.loss))
    self.sum_op = tf.summary.merge(self.summaries)


  def add_image_summary(self, summary_prefix, frames, encoder_length, decoder_future_length, decoder_reconst_length):
    for i in range(decoder_future_length):
      self.summaries.append(tf.summary.image(summary_prefix + '_future_gen_' + str(i + 1),
                                        self.frames_pred[i], max_outputs=1))
      self.summaries.append(tf.summary.image(summary_prefix + '_future_orig_' + str(i + 1),
                                        frames[:, encoder_length + i, :, :, :], max_outputs=1))
    for i in range(decoder_reconst_length):
      self.summaries.append(tf.summary.image(summary_prefix + '_reconst_gen_' + str(i + 1),
                                        self.frames_reconst[i], max_outputs=1))
      self.summaries.append(tf.summary.image(summary_prefix + '_reconst_orig_' + str(i + 1),
                                        frames[:, i, :, :, :], max_outputs=1))

class Initializer:

  def __init__(self, out_dir=None):
    self.status = False
    self.sess = None
    self.threads = None
    self.coord = None
    self.saver = None
    self.saver_restore = None
    self.itr_start = 0

  def start_session(self):
    """Starts a session and initializes all variables. Provides access to session and coordinator"""
    # Start Session and initialize variables
    self.status = True

    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

    self.sess = tf.Session()
    self.sess.run(init_op)

    # Start input enqueue threads
    self.coord = tf.train.Coordinator()
    self.threads = tf.train.start_queue_runners(sess=self.sess, coord=self.coord)

  def stop_session(self):
    """Stops a current session."""
    if self.sess and self.coord:
      self.coord.join(self.threads)
      self.sess.close()
      self.status = False

  def start_saver(self):
    """Constructs a saver and if pretrained model given, loads the model."""
    print('Constructing saver')
    self.saver = tf.train.Saver(max_to_keep=0)

    # restore dumped model if provided
    if FLAGS.pretrained_model:
      print('Restore model from: ' + str(FLAGS.pretrained_model))
      latest_checkpoint = tf.train.latest_checkpoint(FLAGS.pretrained_model)
      self.itr_start = get_iter_from_pretrained_model(latest_checkpoint) + 1
      print('Start with iteration: ' + str(self.itr_start))

      if FLAGS.exclude_from_restoring is not None:
        vars_to_exclude = str(FLAGS.exclude_from_restoring).replace(' ','').split(',')
        global_vars = dict([(v.name, v) for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="train_model")])
        global_vars = remove_items_from_dict(global_vars, vars_to_exclude)
        self.saver_restore = tf.train.Saver(var_list=list(global_vars.values()), max_to_keep=0)
        self.saver_restore.restore(self.sess, latest_checkpoint)
      else:
        self.saver.restore(self.sess, latest_checkpoint)

    return self.saver

def create_model():
  print('Constructing train model and input')
  with tf.variable_scope('train_model', reuse=None) as training_scope:
    train_model = Model('train')

  print('Constructing validation model and input')
  with tf.variable_scope('val_model', reuse=None):
    val_model = Model('valid', reuse_scope=training_scope)
  return train_model, val_model

def train_valid_run(output_dir):
  train_model, val_model = create_model()

  initializer = Initializer()
  initializer.start_session()

  saver = initializer.start_saver()

  summary_writer = tf.summary.FileWriter(output_dir, graph=initializer.sess.graph, flush_secs=10)

  tf.logging.set_verbosity(tf.logging.INFO)
  tf.logging.info(' --- Start Training --- ')
  tf.logging.info(' Iteration, Train_Loss ')

  elapsed_time = 0

  ''' main training loop '''
  try:
    for itr in range(initializer.itr_start, initializer.itr_start + FLAGS.num_iterations):
      try:
        if initializer.coord.should_stop():
          break

        #Training Step on batch
        learning_rate = learning_rate_decay(FLAGS.learning_rate, itr, decay_factor=FLAGS.learning_rate_decay)
        feed_dict = {train_model.learning_rate: learning_rate, train_model.elapsed_time: float(elapsed_time)}

        t = time.time()
        train_loss, _, train_summary_str = initializer.sess.run([train_model.loss, train_model.train_op, train_model.sum_op], feed_dict)
        elapsed_time = time.time() - t

        #validation
        if itr % FLAGS.valid_interval == 1:

          feed_dict = {val_model.learning_rate: 0.0}

          # summary and log
          val_loss, val_summary_str = initializer.sess.run([val_model.loss, val_model.sum_op], feed_dict)

          summary_writer.add_summary(val_summary_str, itr)

          #Print validation loss
          tf.logging.info(' Validation loss at step ' + str(itr) + ':    ' + str(val_loss))

        #dump summary
        if itr % FLAGS.summary_interval == 1:
          summary_writer.add_summary(train_summary_str, itr)

        #save model checkpoint
        if itr % FLAGS.save_interval == 1:
          save_path = saver.save(initializer.sess, os.path.join(output_dir, 'model'), global_step=itr) #TODO also implement save operation in Initializer class
          tf.logging.info(' Saved Model to: ' + str(save_path))

        #Print Interation and loss
        tf.logging.info(' ' + str(itr) + ':    ' + str(train_loss) + ' | %.2f sec'%(elapsed_time))
      except Exception as e:
        tf.logging.info('Training iteration ' + str(itr) + 'failed: ' + str(e.message))
  except tf.errors.OutOfRangeError:
    tf.logging.info('Done training -- iterations limit reached')
  finally:
    # When done, ask the threads to stop.
    initializer.coord.request_stop()

  tf.logging.info(' Saving Model ... ')
  saver.save(initializer.sess, os.path.join(output_dir, 'model'), global_step=initializer.itr_start + FLAGS.num_iterations)

  # necessary for outer (train manager) loop to prevent variable conflicts with previously used graph
  tf.reset_default_graph()
  # Wait for threads to finish.
  initializer.stop_session()


def valid_run(output_dir):
  """ feeds validation batch through the model and stores produced frame sequence as gifs to output_dir
    :param
      output_dir: path to output directory where validation summary and gifs are stored
  """

  #Calculate number of validation samples
  valid_filenames = file_paths_from_directory(FLAGS.path, 'valid*')
  num_valid_samples = input.get_number_of_records(valid_filenames)
  print('Detected %i validation samples' % num_valid_samples)

  _, val_model = create_model()

  initializer = Initializer(output_dir)
  initializer.start_session()
  initializer.start_saver()

  summary_writer = tf.summary.FileWriter(output_dir, graph=initializer.sess.graph, flush_secs=10)

  tf.logging.info(' --- Start validation --- ')

  try:
    feed_dict = {val_model.learning_rate: 0.0}

    val_loss, val_summary_str, output_frames, hidden_representations, labels, metadata, orig_frames = initializer.sess.run(
      [val_model.loss, val_model.sum_op, val_model.output_frames, val_model.hidden_repr, val_model.label, val_model.metadata, val_model.val_batch], feed_dict)

    if 'vector' in FLAGS.valid_mode:
      # store encoder latent vector for analysing

      hidden_repr_dir = create_subfolder(output_dir, 'hidden_repr')
      store_encoder_latent_vector(hidden_repr_dir, hidden_representations, labels, True)

    if 'gif' in FLAGS.valid_mode:
      # summary and log
      val_model.iter_num = 1
      #orig_videos = [orig_frames[i,:,:,:,:] for i in range(orig_frames.shape[0])]
      createGif(orig_frames, labels, output_dir)
      tf.logging.info('Converting validation frame sequences to gif')
      store_output_frames_as_gif(output_frames, labels, output_dir)
      tf.logging.info('Dumped validation gifs in: ' + str(output_dir))

    if 'similarity' in FLAGS.valid_mode:
      print(str(similarity_computations.compute_hidden_representation_similarity(hidden_representations, labels, 'cos')))

    if 'data_frame' in FLAGS.valid_mode:
      #evaluate multiple batches to cover all available validation samples
      for i in range((num_valid_samples//(FLAGS.valid_batch_size * FLAGS.num_gpus))-1):
        hidden_representations_new, labels_new, metadata_new = initializer.sess.run([val_model.hidden_repr, val_model.label, val_model.metadata], feed_dict)
        hidden_representations = np.concatenate((hidden_representations, hidden_representations_new))
        labels = np.concatenate((labels, labels_new))
        metadata = np.concatenate((metadata, metadata_new))

      store_latent_vectors_as_df(output_dir, hidden_representations, labels, metadata)

    summary_writer.add_summary(val_summary_str, 1)


  except tf.errors.OutOfRangeError:
    tf.logging.info('Done producing validation results -- iterations limit reached')
  finally:
    # When done, ask the threads to stop.
    initializer.coord.request_stop()

  # Wait for threads to finish.
  initializer.stop_session()

def tower_operations(video_batch, train=True):
  """
  Build the computation graph from input frame sequences till loss of batch
  :param device number for assining queue runner to CPU
  :param train: boolean that indicates whether train or validation mode
  :return batch loss (scalar)
  """
  #only dropout in train mode
  keep_prob_dropout = FLAGS.keep_prob_dopout if train else 1.0

  frames_pred, frames_reconst, hidden_repr = model.composite_model(video_batch, FLAGS.encoder_length,
                                                                   FLAGS.decoder_future_length,
                                                                   FLAGS.decoder_reconst_length,
                                                                   keep_prob_dropout=keep_prob_dropout,
                                                                   noise_std=FLAGS.noise_std,
                                                                   uniform_init=FLAGS.uniform_init,
                                                                   num_channels=FLAGS.num_channels,
                                                                   fc_conv_layer=FLAGS.fc_layer)

  tower_loss = loss_functions.composite_loss(video_batch, frames_pred, frames_reconst, loss_fun=FLAGS.loss_function,
                                encoder_length=FLAGS.encoder_length,
                                decoder_future_length=FLAGS.decoder_future_length,
                                decoder_reconst_length=FLAGS.decoder_reconst_length)
  return tower_loss, frames_pred, frames_reconst, hidden_repr

def valid_operations(training_scope):
  val_set, video_id_batch, metadata_batch = input.create_batch(FLAGS.path, 'valid', FLAGS.valid_batch_size,
                                                               int(math.ceil(
                                                                 FLAGS.num_iterations / FLAGS.valid_interval) + 10),
                                                               False)
  val_set = tf.cast(val_set, tf.float32)

  frames_pred, frames_reconst, hidden_repr = model.composite_model(val_set, FLAGS.encoder_length,
                                                                   FLAGS.decoder_future_length,
                                                                   FLAGS.decoder_reconst_length,
                                                                   uniform_init=FLAGS.uniform_init,
                                                                   num_channels=FLAGS.num_channels,
                                                                   fc_conv_layer=FLAGS.fc_layer)

  loss = loss_functions.composite_loss(val_set, frames_pred, frames_reconst, loss_fun=FLAGS.loss_function,
                                encoder_length=FLAGS.encoder_length,
                                decoder_future_length=FLAGS.decoder_future_length,
                                decoder_reconst_length=FLAGS.decoder_reconst_length)

  return loss, frames_pred, frames_reconst, hidden_repr, val_set, metadata_batch, video_id_batch

def average_gradients(tower_grads):
  """Calculate the average gradient for each shared variable across all towers.
  Note that this function provides a synchronization point across all towers.
  Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over individual gradients. The inner list is over the gradient
      calculation for each tower.
  Returns:
     List of pairs of (gradient, variable) where the gradient has been averaged
     across all towers.
  """

  average_grads = []

  for grad_and_vars in zip(*tower_grads):
    # Note that each grad_and_vars looks like the following:
    #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    grads = []
    for g, _ in grad_and_vars:
      # Add 0 dimension to the gradients to represent the tower.
      expanded_g = tf.expand_dims(g, 0)

      # Append on a 'tower' dimension which we will average over below.
      grads.append(expanded_g)

    # Average over the 'tower' dimension.
    grad = tf.concat(0, grads)
    grad = tf.reduce_mean(grad, 0)

    # Keep in mind that the Variables are redundant because they are shared
    # across towers. So .. we will just return the first tower's pointer to
    # the Variable.
    v = grad_and_vars[0][1]
    grad_and_var = (grad, v)
    average_grads.append(grad_and_var)
  return average_grads

def average_losses(tower_losses):
  """Calculate the average loss among all towers
  Args:
    tower_losses: List of tf.Tensor skalars denoting the loss at each tower.
  Returns:
     loss: tf.Tensor skalar which is the mean over all losses
  """
  losses = []
  for l in tower_losses:
    # Add 0 dimension to the gradients to represent the tower.
    expanded_l = tf.expand_dims(l, 0)

    # Append on a 'tower' dimension which we will average over below.
    losses.append(expanded_l)

  # Average over the 'tower' dimension.
  loss = tf.concat(0, losses)
  loss = tf.reduce_mean(loss, 0)
  return loss

def main(argv):
  # run validation only
  if FLAGS.valid_only:
    assert FLAGS.pretrained_model
    output_dir = FLAGS.pretrained_model
    tf.logging.info(' --- VALIDATION MODE ONLY --- ')
    print('Reusing provided session directory:', output_dir)
    subdir = create_subfolder(output_dir, 'valid_run')
    print('Storing validation data in:', subdir)
    valid_run(subdir)

  # run training + validation
  else:
    if not FLAGS.pretrained_model:
      # create new session directory
      output_dir = create_session_dir(FLAGS.output_dir)
    else:
      output_dir = FLAGS.pretrained_model
      print('Reusing provided session directory:', output_dir)

    tf.logging.info(' --- TRAIN+VALID MODE --- ')
    write_metainfo(output_dir, model, FLAGS)
    train_valid_run(output_dir)


if __name__ == '__main__':
  app.run()

