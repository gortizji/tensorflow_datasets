from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from absl import app
from absl import flags

import tensorflow as tf
from tensorflow_datasets.core.utils import py_utils
from tensorflow_datasets.testing import fake_data_utils

flags.DEFINE_string('tfds_dir', py_utils.tfds_dir(),
                    'Path to tensorflow_datasets directory')

FLAGS = flags.FLAGS


def _output_dir():
  return os.path.join(FLAGS.tfds_dir, 'testing', 'test_data', 'fake_examples',
                      'malaria','cell_images')


def create_folder(fname):
  images_dir = os.path.join(_output_dir(), fname)
  if not tf.io.gfile.exists(images_dir):
    tf.io.gfile.makedirs(images_dir)
  for i in range(2):
    image_name = 'C189P150ThinF_IMG_20151203_141809_cell_{:03d}.png'.format(i)
    tf.io.gfile.copy(fake_data_utils.get_random_png(),
                     os.path.join(images_dir, image_name),
                     overwrite=True)

def main(argv):
  create_folder('Parasitized')
  create_folder('Uninfected')


if __name__ == '__main__':
  app.run(main)