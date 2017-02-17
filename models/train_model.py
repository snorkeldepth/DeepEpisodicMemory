from tensorflow.examples.tutorials.mnist import input_data
import tensorflow as tf
import numpy as np
import model
import argparse
import os
import data_prep.model_input as input



from tensorflow.python.platform import app
from tensorflow.python.platform import flags

tf.logging.set_verbosity(tf.logging.INFO)

LOSS_FUNCTIONS = ['mse', 'gdl']

FLAGS = flags.FLAGS

# hyperparameters
flags.DEFINE_integer('num_epochs', 100000, 'specify number of training iterations, defaults to 100 000')
flags.DEFINE_string('loss_function', 'gdl', 'specify loss function to minimize, defaults to gdl')
flags.DEFINE_string('batch_size', 50, 'specify the batch size, defaults to 50')
flags.DEFINE_string('mode', 'train' 'specify the mode (train|valid|test), defaults to train')
flags.DEFINE_string('path', '../data/', 'specify the path to where tfrecords are stored, defaults to "../data/"')

flags.DEFINE_string('encoder_length', 5, 'specifies how many images the encoder receives, defaults to 5')
flags.DEFINE_string('decoder_future_length', 5, 'specifies how many images the future prediction decoder receives, defaults to 5')
flags.DEFINE_string('decoder_reconst_length', 5, 'specifies how many images the reconstruction decoder receives, defaults to 5')


def gradient_difference_loss(true, pred, alpha=2.0):
  """description here"""
  tf.assert_equal(tf.shape(true), tf.shape(pred))
  # vertical
  true_pred_diff_vert = tf.pow(tf.abs(difference_gradient(true, vertical=True) - difference_gradient(pred, vertical=True)), alpha)
  # horizontal
  true_pred_diff_hor = tf.pow(tf.abs(difference_gradient(true, vertical=False) - difference_gradient(pred, vertical=False)), alpha)
  # normalization over all dimensions
  return tf.reduce_sum(true_pred_diff_vert) + tf.reduce_sum(true_pred_diff_hor) / tf.to_float(2*tf.size(pred))



def difference_gradient(image, vertical=True):
  # two dimensional tensor
  # rank = ndim in numpy
  #tf.assert_rank(tf.rank(image), 4)

  # careful! begin is zero-based; size is one-based
  if vertical:
    begin0 = [0, 0, 0]
    begin1 = [1, 0, 0]
    size = [tf.shape(image)[1] - 1, tf.shape(image)[2], tf.shape(image)[3]]
  else: # horizontal
    begin0 = [0, 0, 0]
    begin1 = [0, 1, 0]
    size = [tf.shape(image)[1], tf.shape(image)[2] - 1, tf.shape(image)[3]]

  slice0 = tf.slice(image[0, :, :, :], begin0, size)
  slice1 = tf.slice(image[0, :, :, :], begin1, size)
  return tf.abs(tf.sub(slice0, slice1))


def mean_squared_error(true, pred):
  """L2 distance between tensors true and pred.
  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    mean squared error between ground truth and predicted image.
  """
  return tf.reduce_sum(tf.square(true - pred)) / tf.to_float(tf.size(pred))


def peak_signal_to_noise_ratio(true, pred):
  """Image quality metric based on maximal signal power vs. power of the noise.
  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    peak signal to noise ratio (PSNR)
  """
  return 10.0 * tf.log(1.0 / mean_squared_error(true, pred)) / tf.log(10.0)


def decoder_loss(frames_gen, frames_original, loss_fun):
  """Sum of parwise loss between frames of frames_gen and frames_original
    Args:
    frames_gen: array of length=sequence_length of Tensors with each having the shape=(batch size, frame_height, frame_width, num_channels)
    frames_original: Tensor with shape=(batch size, sequence_length, frame_height, frame_width, num_channels)
    loss_fun: loss function type ['mse',...]
  Returns:
    loss: sum (specified) loss between ground truth and predicted frames of provided sequence.
  """
  assert loss_fun in LOSS_FUNCTIONS
  loss = 0.0
  if loss_fun == 'mse':
    for i in range(len(frames_gen)):
      loss += mean_squared_error(frames_original[:, i, :, :, :], frames_gen[i])
  if loss_fun == 'gdl':
    for i in range(len(frames_gen)):
      loss += gradient_difference_loss(frames_original[:, i, :, :, :], frames_gen[i])
  return loss


def decoder_psnr(frames_gen, frames_original, loss_fun):
  """Sum of peak_signal_to_noise_ratio loss between frames of frames_gen and frames_original
     Args:
       frames_gen: array of length=sequence_length of Tensors with each having the shape=(batch size, frame_height, frame_width, num_channels)
       frames_original: Tensor with shape=(batch size, sequence_length, frame_height, frame_width, num_channels)
       loss_fun: loss function type ['mse',...]
     Returns:
       loss: sum of mean squared error between ground truth and predicted frames of provided sequence.
  """
  psnr = 0.0
  for i in range(len(frames_gen)):
    psnr += peak_signal_to_noise_ratio(frames_original[:, i, :, :, :], frames_gen[i])
  return psnr


def composite_loss(original_frames, frames_pred, frames_reconst, loss_fun='mse',
                   encoder_length=FLAGS.encoder_length, decoder_future_length=FLAGS.decoder_future_length,
                   decoder_reconst_length=FLAGS.decoder_reconst_length):

  assert encoder_length <= decoder_reconst_length
  assert loss_fun in LOSS_FUNCTIONS
  frames_original_future = original_frames[:, (encoder_length):(encoder_length + decoder_future_length), :, :, :]
  frames_original_reconst = original_frames[:, (encoder_length - decoder_reconst_length):encoder_length, :, :, :]
  pred_loss = decoder_loss(frames_pred, frames_original_future, loss_fun)
  reconst_loss = decoder_loss(frames_reconst, frames_original_reconst, loss_fun)
  return pred_loss + reconst_loss


def main(unused_argv):
  # mnist = input_data.read_data_sets('MNIST_data', one_hot=True)

  x = tf.placeholder(tf.float32, shape=[None, None, 128, 128, 1])  # 128x128 images

  frames_pred, frames_reconst = model.composite_model(x, FLAGS.encoder_length, FLAGS.decoder_future_length, FLAGS.decoder_reconst_length,
                                                      num_channels=1)

  # Loss Function
  loss = composite_loss(x, frames_pred, frames_reconst, loss_fun=FLAGS.loss_function)

  # choose optimizer
  train_step = tf.train.AdamOptimizer(1e-4).minimize(loss)

  sess = tf.InteractiveSession()

  # start session
  sess.run(tf.global_variables_initializer())


  # run train or test
  #for i in range(FLAGS.num_epochs):
  #  batch = np.random.rand(50, 10, 128, 128, 1)
  #  assert (batch.shape[2] >= (ENCODER_LENGTH + DECODER_FUTURE_LENGTH))
  #  train_step.run(feed_dict={x: batch})
  #  tf.logging.info(str(i))

  """Run a train or test. All files under the given path that match the RegEx "modus*.tfrecords" are used for the batch
  generation in the train or test run. The tfrecords batch element is a video consisting of 20 images and
  4 threads are used for pre-processing the data."""
  batch = input.create_batch(FLAGS.path, FLAGS.modus, FLAGS.batch_size, FLAGS.num_epochs)
  train_step.run(feed_dict={x: batch})
  tf.logging.info() #todo check logger usage


if __name__ == '__main__':
  app.run()

