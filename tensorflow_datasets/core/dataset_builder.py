# coding=utf-8
# Copyright 2022 The TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DatasetBuilder base class."""

from __future__ import annotations

import abc
import dataclasses
import functools
import inspect
import json
import os
import sys
import typing
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Tuple, Type, Union

from absl import logging
from etils import epath
import six
from tensorflow_datasets.core import constants
from tensorflow_datasets.core import dataset_info
from tensorflow_datasets.core import dataset_metadata
from tensorflow_datasets.core import decode
from tensorflow_datasets.core import download
from tensorflow_datasets.core import file_adapters
from tensorflow_datasets.core import logging as tfds_logging
from tensorflow_datasets.core import naming
from tensorflow_datasets.core import reader as reader_lib
from tensorflow_datasets.core import registered
from tensorflow_datasets.core import split_builder as split_builder_lib
from tensorflow_datasets.core import splits as splits_lib
from tensorflow_datasets.core import tf_compat
from tensorflow_datasets.core import units
from tensorflow_datasets.core import utils
from tensorflow_datasets.core.proto import dataset_info_pb2
from tensorflow_datasets.core.utils import file_utils
from tensorflow_datasets.core.utils import gcs_utils
from tensorflow_datasets.core.utils import read_config as read_config_lib
from tensorflow_datasets.core.utils import tree_utils
from tensorflow_datasets.core.utils import type_utils
from tensorflow_datasets.core.utils.lazy_imports_utils import tensorflow as tf
import termcolor

if typing.TYPE_CHECKING:
  from apache_beam.runners import runner

Tree = type_utils.Tree
TreeDict = type_utils.TreeDict
VersionOrStr = Union[utils.Version, str]

FORCE_REDOWNLOAD = download.GenerateMode.FORCE_REDOWNLOAD
REUSE_CACHE_IF_EXISTS = download.GenerateMode.REUSE_CACHE_IF_EXISTS
REUSE_DATASET_IF_EXISTS = download.GenerateMode.REUSE_DATASET_IF_EXISTS

GCS_HOSTED_MSG = """\
Dataset %s is hosted on GCS. It will automatically be downloaded to your
local data directory. If you'd instead prefer to read directly from our public
GCS bucket (recommended if you're running on GCP), you can instead pass
`try_gcs=True` to `tfds.load` or set `data_dir=gs://tfds-data/datasets`.
"""


@dataclasses.dataclass(eq=False)
class BuilderConfig:
  """Base class for `DatasetBuilder` data configuration.

  DatasetBuilder subclasses with data configuration options should subclass
  `BuilderConfig` and add their own properties.
  """
  # TODO(py3.10): Should update dataclass to be:
  # * Frozen (https://bugs.python.org/issue32953)
  # * Kwargs-only (https://bugs.python.org/issue33129)

  name: str
  version: Optional[VersionOrStr] = None
  release_notes: Optional[Dict[str, str]] = None
  supported_versions: List[VersionOrStr] = dataclasses.field(
      default_factory=list)
  description: Optional[str] = None

  @classmethod
  def from_dataset_info(
      cls,
      info_proto: dataset_info_pb2.DatasetInfo) -> Optional["BuilderConfig"]:
    if not info_proto.config_name:
      return None
    return BuilderConfig(
        name=info_proto.config_name,
        description=info_proto.config_description,
        version=info_proto.version,
        release_notes=info_proto.release_notes or {},
    )


def _get_builder_datadir_path(builder_cls: Type[Any]) -> epath.Path:
  """Returns the path to ConfigBuilder data dir.

  Args:
    builder_cls: a Builder class.

  Returns:
    The path to directory where the config data files of `builders_cls` can be
    read from. e.g. "/usr/lib/[...]/tensorflow_datasets/datasets/foo".
  """
  pkg_names = builder_cls.__module__.split(".")
  # -1 to remove the xxx.py python file.
  return epath.resource_path(pkg_names[0]).joinpath(*pkg_names[1:-1])


class DatasetBuilder(registered.RegisteredDataset):
  """Abstract base class for all datasets.

  `DatasetBuilder` has 3 key methods:

    * `DatasetBuilder.info`: documents the dataset, including feature
      names, types, and shapes, version, splits, citation, etc.
    * `DatasetBuilder.download_and_prepare`: downloads the source data
      and writes it to disk.
    * `DatasetBuilder.as_dataset`: builds an input pipeline using
      `tf.data.Dataset`s.

  **Configuration**: Some `DatasetBuilder`s expose multiple variants of the
  dataset by defining a `tfds.core.BuilderConfig` subclass and accepting a
  config object (or name) on construction. Configurable datasets expose a
  pre-defined set of configurations in `DatasetBuilder.builder_configs`.

  Typical `DatasetBuilder` usage:

  ```python
  mnist_builder = tfds.builder("mnist")
  mnist_info = mnist_builder.info
  mnist_builder.download_and_prepare()
  datasets = mnist_builder.as_dataset()

  train_dataset, test_dataset = datasets["train"], datasets["test"]
  assert isinstance(train_dataset, tf.data.Dataset)

  # And then the rest of your input pipeline
  train_dataset = train_dataset.repeat().shuffle(1024).batch(128)
  train_dataset = train_dataset.prefetch(2)
  features = tf.compat.v1.data.make_one_shot_iterator(train_dataset).get_next()
  image, label = features['image'], features['label']
  ```
  """

  # Semantic version of the dataset (ex: tfds.core.Version('1.2.0'))
  VERSION = None

  # Release notes
  # Metadata only used for documentation. Should be a dict[version,description]
  # Multi-lines are automatically dedent
  RELEASE_NOTES: ClassVar[Dict[str, str]] = {}

  # List dataset versions which can be loaded using current code.
  # Data can only be prepared with canonical VERSION or above.
  SUPPORTED_VERSIONS = []

  # Named configurations that modify the data generated by download_and_prepare.
  BUILDER_CONFIGS = []

  # Name of the builder config that should be used in case the user doesn't
  # specify a config when loading a dataset. If None, then the first config in
  # `BUILDER_CONFIGS` is used.
  DEFAULT_BUILDER_CONFIG_NAME: Optional[str] = None

  # Must be set for datasets that use 'manual_dir' functionality - the ones
  # that require users to do additional steps to download the data
  # (this is usually due to some external regulations / rules).
  #
  # This field should contain a string with user instructions, including
  # the list of files that should be present. It will be
  # displayed in the dataset documentation.
  MANUAL_DOWNLOAD_INSTRUCTIONS = None

  # Optional max number of simultaneous downloads. Setting this value will
  # override download config settings if necessary.
  MAX_SIMULTANEOUS_DOWNLOADS: Optional[int] = None

  # If not set, pkg_dir_path is inferred. However, if user of class knows better
  # then this can be set directly before init, to avoid heuristic inferences.
  # Example: `imported_builder_cls` function in `registered.py` module sets it.
  pkg_dir_path: Optional[epath.Path] = None

  @classmethod
  def _get_pkg_dir_path(cls) -> epath.Path:
    """Returns class pkg_dir_path, infer it first if not set."""
    # We use cls.__dict__.get to check whether `pkg_dir_path` attribute has been
    # set on the class, and not on a parent class. If we were accessing the
    # attribute directly, a dataset Builder inheriting from another would read
    # its metadata from its parent directory (which would be wrong).
    if cls.__dict__.get("pkg_dir_path") is None:
      cls.pkg_dir_path = _get_builder_datadir_path(cls)
    return cls.pkg_dir_path

  @classmethod
  def get_metadata(cls) -> dataset_metadata.DatasetMetadata:
    """Returns metadata (README, CITATIONS, ...) specified in config files.

    The config files are read from the same package where the DatasetBuilder has
    been defined, so those metadata might be wrong for legacy builders.
    """
    return dataset_metadata.load(cls._get_pkg_dir_path())

  @tfds_logging.builder_init()
  def __init__(
      self,
      *,
      data_dir: Optional[epath.PathLike] = None,
      config: Union[None, str, BuilderConfig] = None,
      version: Union[None, str, utils.Version] = None,
  ):
    """Constructs a DatasetBuilder.

    Callers must pass arguments as keyword arguments.

    Args:
      data_dir: directory to read/write data. Defaults to the value of the
        environment variable TFDS_DATA_DIR, if set, otherwise falls back to
        datasets are stored.
      config: `tfds.core.BuilderConfig` or `str` name, optional configuration
        for the dataset that affects the data generated on disk. Different
        `builder_config`s will have their own subdirectories and versions.
      version: Optional version at which to load the dataset. An error is raised
        if specified version cannot be satisfied. Eg: '1.2.3', '1.2.*'. The
        special value "experimental_latest" will use the highest version, even
        if not default. This is not recommended unless you know what you are
        doing, as the version could be broken.
    """
    if data_dir:
      data_dir = os.fspath(data_dir)  # Pathlib -> str
    # For pickling:
    self._original_state = dict(
        data_dir=data_dir, config=config, version=version)
    # To do the work:
    self._builder_config = self._create_builder_config(config)
    # Extract code version (VERSION or config)
    self._version = self._pick_version(version)
    # Compute the base directory (for download) and dataset/version directory.
    self._data_dir_root, self._data_dir = self._build_data_dir(data_dir)
    if tf.io.gfile.exists(self._data_dir):
      self.info.read_from_directory(self._data_dir)
    else:  # Use the code version (do not restore data)
      self.info.initialize_from_bucket()

  @utils.classproperty
  @classmethod
  @utils.memoize()
  def code_path(cls) -> Optional[epath.Path]:
    """Returns the path to the file where the Dataset class is located.

    Note: As the code can be run inside zip file. The returned value is
    a `Path` by default. Use `tfds.core.utils.to_write_path()` to cast
    the path into `Path`.

    Returns:
      path: pathlib.Path like abstraction
    """
    modules = cls.__module__.split(".")
    if len(modules) >= 2:  # Filter `__main__`, `python my_dataset.py`,...
      # If the dataset can be loaded from a module, use this to support zipapp.
      # Note: `utils.resource_path` will return either `zipfile.Path` (for
      # zipapp) or `pathlib.Path`.
      try:
        path = epath.resource_path(modules[0])
      except TypeError:  # Module is not a package
        pass
      else:
        # For dynamically added modules, `importlib.resources` returns
        # `pathlib.Path('.')` rather than the real path, so filter those by
        # checking for `parts`.
        # Check for `zipfile.Path` (`ResourcePath`) as it does not have `.parts`
        if isinstance(path, epath.resource_utils.ResourcePath) or path.parts:
          modules[-1] += ".py"
          return path.joinpath(*modules[1:])
    # Otherwise, fallback to `pathlib.Path`. For non-zipapp, it should be
    # equivalent to the above return.
    try:
      filepath = inspect.getfile(cls)
    except TypeError:
      # Could happen when the class is defined in Colab.
      return None
    else:
      return epath.Path(filepath)

  def __getstate__(self):
    return self._original_state

  def __setstate__(self, state):
    self.__init__(**state)

  @utils.memoized_property
  def canonical_version(self) -> utils.Version:
    return cannonical_version_for_config(self, self._builder_config)

  @utils.memoized_property
  def supported_versions(self):
    if self._builder_config and self._builder_config.supported_versions:
      return self._builder_config.supported_versions
    else:
      return self.SUPPORTED_VERSIONS

  @utils.memoized_property
  def versions(self) -> List[utils.Version]:
    """Versions (canonical + availables), in preference order."""
    return [
        utils.Version(v) if isinstance(v, six.string_types) else v
        for v in [self.canonical_version] + self.supported_versions
    ]

  def _pick_version(
      self, requested_version: Union[str, utils.Version]) -> utils.Version:
    """Returns utils.Version instance, or raise AssertionError."""
    # Validate that `canonical_version` is correctly defined
    assert self.canonical_version
    # If requested_version is of type utils.Version, convert to its string
    # representation. This is necessary to properly execute the equality check
    # with "experimental_latest".
    if isinstance(requested_version, utils.Version):
      requested_version = str(requested_version)
    if requested_version == "experimental_latest":
      return max(self.versions)
    for version in self.versions:
      if requested_version is None or version.match(requested_version):
        return version
    available_versions = [str(v) for v in self.versions]
    msg = "Dataset {} cannot be loaded at version {}, only: {}.".format(
        self.name, requested_version, ", ".join(available_versions))
    raise AssertionError(msg)

  @property
  def version(self) -> utils.Version:
    return self._version

  @property
  def release_notes(self) -> Dict[str, str]:
    if self.builder_config and self.builder_config.release_notes:
      return self.builder_config.release_notes
    else:
      return self.RELEASE_NOTES

  @property
  def data_dir(self) -> str:
    return self._data_dir

  @property
  def data_path(self) -> epath.Path:
    # Instead, should make `_data_dir` be Path everywhere
    return epath.Path(self._data_dir)

  @utils.classproperty
  @classmethod
  def _checksums_path(cls) -> Optional[epath.Path]:
    """Returns the checksums path."""
    # Used:
    # * To load the checksums (in url_infos)
    # * To save the checksums (in DownloadManager)
    if not cls.code_path:
      return None
    new_path = cls.code_path.parent / "checksums.tsv"
    # Checksums of legacy datasets are located in a separate dir.
    legacy_path = utils.tfds_path() / "url_checksums" / f"{cls.name}.txt"
    if (
        # zipfile.Path does not have `.parts`. Additionally, `os.fspath`
        # will extract the file, so use `str`.
        "tensorflow_datasets" in str(new_path) and legacy_path.exists() and
        not new_path.exists()):
      return legacy_path
    else:
      return new_path

  @utils.classproperty
  @classmethod
  @functools.lru_cache(maxsize=None)
  def url_infos(cls) -> Optional[Dict[str, download.checksums.UrlInfo]]:
    """Load `UrlInfo` from the given path."""
    # Note: If the dataset is downloaded with `record_checksums=True`, urls
    # might be updated but `url_infos` won't as it is memoized.

    # Search for the url_info file.
    checksums_path = cls._checksums_path
    # If url_info file is found, load the urls
    if checksums_path and checksums_path.exists():
      return download.checksums.load_url_infos(checksums_path)
    else:
      return None

  @property
  @tfds_logging.builder_info()
  # Warning: This unbounded cache is required for correctness. See b/238762111.
  @functools.lru_cache(maxsize=None)
  def info(self) -> dataset_info.DatasetInfo:
    """`tfds.core.DatasetInfo` for this builder."""
    # Ensure .info hasn't been called before versioning is set-up
    # Otherwise, backward compatibility cannot be guaranteed as some code will
    # depend on the code version instead of the restored data version
    if not getattr(self, "_version", None):
      # Message for developers creating new dataset. Will trigger if they are
      # using .info in the constructor before calling super().__init__
      raise AssertionError(
          "Info should not been called before version has been defined. "
          "Otherwise, the created .info may not match the info version from "
          "the restored dataset.")
    info = self._info()
    if not isinstance(info, dataset_info.DatasetInfo):
      raise TypeError(
          "DatasetBuilder._info should returns `tfds.core.DatasetInfo`, not "
          f" {type(info)}.")
    return info

  @utils.classproperty
  @classmethod
  def default_builder_config(cls) -> Optional[BuilderConfig]:
    return _get_default_config(
        builder_configs=cls.BUILDER_CONFIGS,
        default_config_name=cls.DEFAULT_BUILDER_CONFIG_NAME)

  def get_default_builder_config(self) -> Optional[BuilderConfig]:
    """Returns the default builder config if there is one.

    Note that for dataset builders that cannot use the `cls.BUILDER_CONFIGS`, we
    need a method that uses the instance to get `BUILDER_CONFIGS` and
    `DEFAULT_BUILDER_CONFIG_NAME`.

    Returns:
      the default builder config if there is one
    """
    return _get_default_config(
        builder_configs=self.BUILDER_CONFIGS,
        default_config_name=self.DEFAULT_BUILDER_CONFIG_NAME)

  def get_reference(
      self,
      namespace: Optional[str] = None,
  ) -> naming.DatasetReference:
    """Returns a reference to the dataset produced by this dataset builder.

    Includes the config if specified, the version, and the data_dir that should
    contain this dataset.

    Arguments:
      namespace: if this dataset is a community dataset, and therefore has a
        namespace, then the namespace must be provided such that it can be set
        in the reference. Note that a dataset builder is not aware that it is
        part of a namespace.

    Returns:
      a reference to this instantiated builder.
    """
    if self.builder_config:
      config = self.builder_config.name
    else:
      config = None
    return naming.DatasetReference(
        dataset_name=self.name,
        namespace=namespace,
        config=config,
        version=self.version,
        data_dir=epath.Path(self._data_dir_root))

  def download_and_prepare(
      self,
      *,
      download_dir: Optional[str] = None,
      download_config: Optional[download.DownloadConfig] = None,
      file_format: Union[None, str, file_adapters.FileFormat] = None,
  ) -> None:
    """Downloads and prepares dataset for reading.

    Args:
      download_dir: `str`, directory where downloaded files are stored. Defaults
        to "~/tensorflow-datasets/downloads".
      download_config: `tfds.download.DownloadConfig`, further configuration for
        downloading and preparing dataset.
      file_format: optional `str` or `file_adapters.FileFormat`, format of the
        record files in which the dataset will be written.

    Raises:
      IOError: if there is not enough disk space available.
      RuntimeError: when the config cannot be found.
    """

    download_config = download_config or download.DownloadConfig()
    data_exists = tf.io.gfile.exists(self._data_dir)
    if data_exists and download_config.download_mode == REUSE_DATASET_IF_EXISTS:
      logging.info("Reusing dataset %s (%s)", self.name, self._data_dir)
      return
    elif data_exists and download_config.download_mode == REUSE_CACHE_IF_EXISTS:
      logging.info("Deleting pre-existing dataset %s (%s)", self.name,
                   self._data_dir)
      epath.Path(self._data_dir).rmtree()  # Delete pre-existing data.
      data_exists = tf.io.gfile.exists(self._data_dir)

    if self.version.tfds_version_to_prepare:
      available_to_prepare = ", ".join(
          str(v) for v in self.versions if not v.tfds_version_to_prepare)
      raise AssertionError(
          "The version of the dataset you are trying to use ({}:{}) can only "
          "be generated using TFDS code synced @ {} or earlier. Either sync to "
          "that version of TFDS to first prepare the data or use another "
          "version of the dataset (available for `download_and_prepare`: "
          "{}).".format(self.name, self.version,
                        self.version.tfds_version_to_prepare,
                        available_to_prepare))

    # Only `cls.VERSION` or `experimental_latest` versions can be generated.
    # Otherwise, users may accidentally generate an old version using the
    # code from newer versions.
    installable_versions = {
        str(v) for v in (self.canonical_version, max(self.versions))
    }
    if str(self.version) not in installable_versions:
      msg = ("The version of the dataset you are trying to use ({}) is too "
             "old for this version of TFDS so cannot be generated.").format(
                 self.info.full_name)
      if self.version.tfds_version_to_prepare:
        msg += (
            "{} can only be generated using TFDS code synced @ {} or earlier "
            "Either sync to that version of TFDS to first prepare the data or "
            "use another version of the dataset. ").format(
                self.version, self.version.tfds_version_to_prepare)
      else:
        msg += (
            "Either sync to a previous version of TFDS to first prepare the "
            "data or use another version of the dataset. ")
      msg += "Available for `download_and_prepare`: {}".format(
          list(sorted(installable_versions)))
      raise ValueError(msg)

    # Currently it's not possible to overwrite the data because it would
    # conflict with versioning: If the last version has already been generated,
    # it will always be reloaded and data_dir will be set at construction.
    if data_exists:
      raise ValueError(
          "Trying to overwrite an existing dataset {} at {}. A dataset with "
          "the same version {} already exists. If the dataset has changed, "
          "please update the version number.".format(self.name, self._data_dir,
                                                     self.version))

    logging.info("Generating dataset %s (%s)", self.name, self._data_dir)
    if not utils.has_sufficient_disk_space(
        self.info.dataset_size + self.info.download_size,
        directory=self._data_dir_root):
      raise IOError(
          "Not enough disk space. Needed: {} (download: {}, generated: {})"
          .format(
              self.info.dataset_size + self.info.download_size,
              self.info.download_size,
              self.info.dataset_size,
          ))
    self._log_download_bytes()

    dl_manager = self._make_download_manager(
        download_dir=download_dir,
        download_config=download_config,
    )

    # Maybe save the `builder_cls` metadata common to all builder configs.
    if self.BUILDER_CONFIGS:
      default_builder_config = self.get_default_builder_config()
      if default_builder_config is None:
        raise RuntimeError("Could not find default builder config while there "
                           "are builder configs!")
      _save_default_config_name(
          # `data_dir/ds_name/config/version/` -> `data_dir/ds_name/`
          common_dir=self.data_path.parent.parent,
          default_config_name=default_builder_config.name,
      )

    # If the file format was specified, set it in the info such that it is used
    # to generate the files.
    if file_format:
      self.info.set_file_format(file_format, override=True)

    # Create a tmp dir and rename to self._data_dir on successful exit.
    with utils.incomplete_dir(self._data_dir) as tmp_data_dir:
      # Temporarily assign _data_dir to tmp_data_dir to avoid having to forward
      # it to every sub function.
      with utils.temporary_assignment(self, "_data_dir", tmp_data_dir):
        if (download_config.try_download_gcs and
            gcs_utils.is_dataset_on_gcs(self.info.full_name)):
          logging.info(GCS_HOSTED_MSG, self.name)
          gcs_utils.download_gcs_dataset(
              dataset_name=self.info.full_name,
              local_dataset_dir=self._data_dir)
          self.info.read_from_directory(self._data_dir)
        else:
          self._download_and_prepare(
              dl_manager=dl_manager,
              download_config=download_config,
          )

          # NOTE: If modifying the lines below to put additional information in
          # DatasetInfo, you'll likely also want to update
          # DatasetInfo.read_from_directory to possibly restore these attributes
          # when reading from package data.
          self.info.download_size = dl_manager.downloaded_size
          # Write DatasetInfo to disk, even if we haven't computed statistics.
          self.info.write_to_directory(self._data_dir)
      # The generated DatasetInfo contains references to `tmp_data_dir`
      self.info.update_data_dir(self._data_dir)

    # Clean up incomplete files from preempted workers.
    deleted_incomplete_files = []
    for f in self.data_path.iterdir():
      if utils.is_incomplete_file(f):
        deleted_incomplete_files.append(os.fspath(f))
        f.unlink()
    if deleted_incomplete_files:
      logging.info("Deleted %d incomplete files. A small selection: %s",
                   len(deleted_incomplete_files),
                   "\n".join(deleted_incomplete_files[:3]))

    self._log_download_done()

  @tfds_logging.as_dataset()
  def as_dataset(
      self,
      split: Optional[Tree[splits_lib.SplitArg]] = None,
      *,
      batch_size: Optional[int] = None,
      shuffle_files: bool = False,
      decoders: Optional[TreeDict[decode.partial_decode.DecoderArg]] = None,
      read_config: Optional[read_config_lib.ReadConfig] = None,
      as_supervised: bool = False,
  ):
    # pylint: disable=line-too-long
    """Constructs a `tf.data.Dataset`.

    Callers must pass arguments as keyword arguments.

    The output types vary depending on the parameters. Examples:

    ```python
    builder = tfds.builder('imdb_reviews')
    builder.download_and_prepare()

    # Default parameters: Returns the dict of tf.data.Dataset
    ds_all_dict = builder.as_dataset()
    assert isinstance(ds_all_dict, dict)
    print(ds_all_dict.keys())  # ==> ['test', 'train', 'unsupervised']

    assert isinstance(ds_all_dict['test'], tf.data.Dataset)
    # Each dataset (test, train, unsup.) consists of dictionaries
    # {'label': <tf.Tensor: .. dtype=int64, numpy=1>,
    #  'text': <tf.Tensor: .. dtype=string, numpy=b"I've watched the movie ..">}
    # {'label': <tf.Tensor: .. dtype=int64, numpy=1>,
    #  'text': <tf.Tensor: .. dtype=string, numpy=b'If you love Japanese ..'>}

    # With as_supervised: tf.data.Dataset only contains (feature, label) tuples
    ds_all_supervised = builder.as_dataset(as_supervised=True)
    assert isinstance(ds_all_supervised, dict)
    print(ds_all_supervised.keys())  # ==> ['test', 'train', 'unsupervised']

    assert isinstance(ds_all_supervised['test'], tf.data.Dataset)
    # Each dataset (test, train, unsup.) consists of tuples (text, label)
    # (<tf.Tensor: ... dtype=string, numpy=b"I've watched the movie ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    # (<tf.Tensor: ... dtype=string, numpy=b"If you love Japanese ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)

    # Same as above plus requesting a particular split
    ds_test_supervised = builder.as_dataset(as_supervised=True, split='test')
    assert isinstance(ds_test_supervised, tf.data.Dataset)
    # The dataset consists of tuples (text, label)
    # (<tf.Tensor: ... dtype=string, numpy=b"I've watched the movie ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    # (<tf.Tensor: ... dtype=string, numpy=b"If you love Japanese ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    ```

    Args:
      split: Which split of the data to load (e.g. `'train'`, `'test'`,
        `['train', 'test']`, `'train[80%:]'`,...). See our [split API
        guide](https://www.tensorflow.org/datasets/splits). If `None`, will
        return all splits in a `Dict[Split, tf.data.Dataset]`.
      batch_size: `int`, batch size. Note that variable-length features will be
        0-padded if `batch_size` is set. Users that want more custom behavior
        should use `batch_size=None` and use the `tf.data` API to construct a
        custom pipeline. If `batch_size == -1`, will return feature dictionaries
        of the whole dataset with `tf.Tensor`s instead of a `tf.data.Dataset`.
      shuffle_files: `bool`, whether to shuffle the input files. Defaults to
        `False`.
      decoders: Nested dict of `Decoder` objects which allow to customize the
        decoding. The structure should match the feature structure, but only
        customized feature keys need to be present. See [the
        guide](https://github.com/tensorflow/datasets/blob/master/docs/decode.md)
        for more info.
      read_config: `tfds.ReadConfig`, Additional options to configure the input
        pipeline (e.g. seed, num parallel reads,...).
      as_supervised: `bool`, if `True`, the returned `tf.data.Dataset` will have
        a 2-tuple structure `(input, label)` according to
        `builder.info.supervised_keys`. If `False`, the default, the returned
        `tf.data.Dataset` will have a dictionary with all the features.

    Returns:
      `tf.data.Dataset`, or if `split=None`, `dict<key: tfds.Split, value:
      tfds.data.Dataset>`.

      If `batch_size` is -1, will return feature dictionaries containing
      the entire dataset in `tf.Tensor`s instead of a `tf.data.Dataset`.
    """
    # pylint: enable=line-too-long
    if not tf.io.gfile.exists(self._data_dir):
      raise AssertionError(
          ("Dataset %s: could not find data in %s. Please make sure to call "
           "dataset_builder.download_and_prepare(), or pass download=True to "
           "tfds.load() before trying to access the tf.data.Dataset object.") %
          (self.name, self._data_dir_root))

    # By default, return all splits
    if split is None:
      split = {s: s for s in self.info.splits}

    read_config = read_config or read_config_lib.ReadConfig()

    # Create a dataset for each of the given splits
    build_single_dataset = functools.partial(
        self._build_single_dataset,
        shuffle_files=shuffle_files,
        batch_size=batch_size,
        decoders=decoders,
        read_config=read_config,
        as_supervised=as_supervised,
    )
    all_ds = tree_utils.map_structure(build_single_dataset, split)
    return all_ds

  def _build_single_dataset(
      self,
      split: splits_lib.Split,
      batch_size: Optional[int],
      shuffle_files: bool,
      decoders: Optional[TreeDict[decode.partial_decode.DecoderArg]],
      read_config: read_config_lib.ReadConfig,
      as_supervised: bool,
  ) -> tf.data.Dataset:
    """as_dataset for a single split."""
    wants_full_dataset = batch_size == -1
    if wants_full_dataset:
      batch_size = self.info.splits.total_num_examples or sys.maxsize

    # Build base dataset
    ds = self._as_dataset(
        split=split,
        shuffle_files=shuffle_files,
        decoders=decoders,
        read_config=read_config,
    )
    # Auto-cache small datasets which are small enough to fit in memory.
    if self._should_cache_ds(
        split=split, shuffle_files=shuffle_files, read_config=read_config):
      ds = ds.cache()

    if batch_size:
      # Use padded_batch so that features with unknown shape are supported.
      ds = ds.padded_batch(batch_size, tf.compat.v1.data.get_output_shapes(ds))

    if as_supervised:
      if not self.info.supervised_keys:
        raise ValueError(
            f"as_supervised=True but {self.name} does not support a supervised "
            "structure.")

      def lookup_nest(features: Dict[str, Any]) -> Tuple[Any, ...]:
        """Converts `features` to the structure described by `supervised_keys`.

        Note that there is currently no way to access features in nested
        feature dictionaries.

        Args:
          features: dictionary of features

        Returns:
          A tuple with elements structured according to `supervised_keys`
        """
        return tree_utils.map_structure(lambda key: features[key],
                                        self.info.supervised_keys)

      ds = ds.map(lookup_nest)

    # Add prefetch by default
    if not read_config.skip_prefetch:
      ds = ds.prefetch(tf.data.experimental.AUTOTUNE)

    if wants_full_dataset:
      return tf_compat.get_single_element(ds)
    return ds

  def _should_cache_ds(self, split, shuffle_files, read_config) -> bool:
    """Returns True if TFDS should auto-cache the dataset."""
    # The user can explicitly opt-out from auto-caching
    if not read_config.try_autocache:
      return False

    # Skip datasets with unknown size.
    # Even by using heuristic with `download_size` and
    # `MANUAL_DOWNLOAD_INSTRUCTIONS`, it wouldn't catch datasets which hardcode
    # the non-processed data-dir, nor DatasetBuilder not based on tf-record.
    if not self.info.dataset_size:
      return False

    # Do not cache big datasets
    # Instead of using the global size, we could infer the requested bytes:
    # `self.info.splits[split].num_bytes`
    # The info is available for full splits, and could be approximated
    # for subsplits `train[:50%]`.
    # However if the user is creating multiple small splits from a big
    # dataset, those could adds up and fill up the entire RAM.
    # 250 MiB is arbitrary picked. For comparison, Cifar10 is about 150 MiB.
    if self.info.dataset_size > 250 * units.MiB:
      return False

    # We do not want to cache data which has more than one shards when
    # shuffling is enabled, as this would effectively disable shuffling.
    # An exception is for single shard (as shuffling is a no-op).
    # Another exception is if reshuffle is disabled (shuffling already cached)
    num_shards = len(self.info.splits[split].file_instructions)
    if (shuffle_files and
        # Shuffling only matter when reshuffle is True or None (default)
        read_config.shuffle_reshuffle_each_iteration is not False and  # pylint: disable=g-bool-id-comparison
        num_shards > 1):
      return False

    # If the dataset satisfy all the right conditions, activate autocaching.
    return True

  def _relative_data_dir(self, with_version: bool = True) -> str:
    """Relative path of this dataset in data_dir."""
    builder_data_dir = self.name
    builder_config = self._builder_config
    if builder_config:
      builder_data_dir = os.path.join(builder_data_dir, builder_config.name)
    if not with_version:
      return builder_data_dir

    version_data_dir = os.path.join(builder_data_dir, str(self._version))
    return version_data_dir

  def _build_data_dir(self, given_data_dir: Optional[str]):
    """Return the data directory for the current version.

    Args:
      given_data_dir: `Optional[str]`, root `data_dir` passed as `__init__`
        argument.

    Returns:
      data_dir_root: `str`, The root dir containing all datasets, downloads,...
      data_dir: `str`, The version data_dir
        (e.g. `<data_dir_root>/<ds_name>/<config>/<version>`)
    """
    builder_dir = self._relative_data_dir(with_version=False)
    version_dir = self._relative_data_dir(with_version=True)

    default_data_dir = file_utils.get_default_data_dir(
        given_data_dir=given_data_dir)
    all_data_dirs = file_utils.list_data_dirs(
        given_data_dir=given_data_dir, dataset=self.name)

    all_versions = set()
    requested_version_dirs = {}
    for data_dir_root in all_data_dirs:
      # List all existing versions
      full_builder_dir = os.path.join(data_dir_root, builder_dir)
      data_dir_versions = set(utils.version.list_all_versions(full_builder_dir))
      # Check for existance of the requested version
      if self.version in data_dir_versions:
        requested_version_dirs[data_dir_root] = os.path.join(
            data_dir_root, version_dir)
      all_versions.update(data_dir_versions)

    if len(requested_version_dirs) > 1:
      raise ValueError(
          "Dataset was found in more than one directory: {}. Please resolve "
          "the ambiguity by explicitly specifying `data_dir=`."
          "".format(requested_version_dirs.values()))
    elif len(requested_version_dirs) == 1:  # The dataset is found once
      return next(iter(requested_version_dirs.items()))

    # No dataset found, use default directory
    data_dir = os.path.join(default_data_dir, version_dir)
    if all_versions:
      logging.warning(
          "Found a different version of the requested dataset:\n"
          "%s\n"
          "Using %s instead.", "\n".join(str(v) for v in sorted(all_versions)),
          data_dir)
    return default_data_dir, data_dir

  def _log_download_done(self) -> None:
    msg = (f"Dataset {self.name} downloaded and prepared to {self._data_dir}. "
           "Subsequent calls will reuse this data.")
    termcolor.cprint(msg, attrs=["bold"])

  def _log_download_bytes(self) -> None:
    # Print is intentional: we want this to always go to stdout so user has
    # information needed to cancel download/preparation if needed.
    # This comes right before the progress bar.
    termcolor.cprint(
        f"Downloading and preparing dataset {self.info.download_size} "
        f"(download: {self.info.download_size}, "
        f"generated: {self.info.dataset_size}, "
        f"total: {self.info.download_size + self.info.dataset_size}) "
        f"to {self._data_dir}...",
        attrs=["bold"],
    )

  def dataset_info_from_configs(self, **kwargs):
    """Returns the DatasetInfo using given kwargs and config files.

    Sub-class should call this and add information not present in config files
    using kwargs directly passed to tfds.core.DatasetInfo object.

    If information is present both in passed arguments and config files, config
    files will prevail.

    Args:
      **kwargs: kw args to pass to DatasetInfo directly.
    """
    metadata = self.get_metadata()
    if metadata.description:
      kwargs["description"] = metadata.description
    if metadata.citation:
      kwargs["citation"] = metadata.citation
    return dataset_info.DatasetInfo(builder=self, **kwargs)

  @abc.abstractmethod
  @utils.docs.doc_private
  def _info(self) -> dataset_info.DatasetInfo:
    """Returns the `tfds.core.DatasetInfo` object.

    This function is called once and the result is cached for all
    following calls.

    Returns:
      dataset_info: The dataset metadata.
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _download_and_prepare(
      self,
      dl_manager: download.DownloadManager,
      download_config: Optional[download.DownloadConfig] = None,
  ) -> None:
    """Downloads and prepares dataset for reading.

    Internal implementation to overwrite when inheriting from DatasetBuilder.
    Called when `builder.download_and_prepare` is called.
    It should download all required data and generate
    the pre-processed datasets files.

    Args:
      dl_manager: `tfds.download.DownloadManager` used to download and cache
        data.
      download_config: `DownloadConfig`, Additional options.
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _as_dataset(
      self,
      split: splits_lib.Split,
      decoders: Optional[TreeDict[decode.partial_decode.DecoderArg]] = None,
      read_config: Optional[read_config_lib.ReadConfig] = None,
      shuffle_files: bool = False,
  ) -> tf.data.Dataset:
    """Constructs a `tf.data.Dataset`.

    Internal implementation to overwrite when inheriting from DatasetBuilder.
    Called when `builder.as_dataset` is called.
    It should read the pre-processed datasets files and generate
    the `tf.data.Dataset` object.

    Args:
      split: `tfds.Split` which subset of the data to read.
      decoders: Nested structure of `Decoder` object to customize the dataset
        decoding.
      read_config: `tfds.ReadConfig`
      shuffle_files: `bool`, whether to shuffle the input files. Optional,
        defaults to `False`.

    Returns:
      `tf.data.Dataset`
    """
    raise NotImplementedError

  def _make_download_manager(
      self,
      download_dir,
      download_config,
  ) -> download.DownloadManager:
    """Creates a new download manager object."""
    download_dir = (
        download_dir or os.path.join(self._data_dir_root, "downloads"))
    extract_dir = (
        download_config.extract_dir or os.path.join(download_dir, "extracted"))
    manual_dir = (
        download_config.manual_dir or os.path.join(download_dir, "manual"))

    if download_config.register_checksums:
      # Note: Error will be raised here if user try to record checksums
      # from a `zipapp`
      register_checksums_path = utils.to_write_path(self._checksums_path)
    else:
      register_checksums_path = None

    max_simultaneous_downloads = (
        download_config.override_max_simultaneous_downloads or
        self.MAX_SIMULTANEOUS_DOWNLOADS)
    if (max_simultaneous_downloads and self.MAX_SIMULTANEOUS_DOWNLOADS and
        self.MAX_SIMULTANEOUS_DOWNLOADS < max_simultaneous_downloads):
      logging.warning(
          "The dataset %r sets `MAX_SIMULTANEOUS_DOWNLOADS`=%s. The download "
          "config overrides it with value `override_max_simultaneous_downloads`"
          "=%s. Using the higher value might cause `ConnectionRefusedError`",
          self.name, self.MAX_SIMULTANEOUS_DOWNLOADS,
          download_config.override_max_simultaneous_downloads)

    return download.DownloadManager(
        download_dir=download_dir,
        extract_dir=extract_dir,
        manual_dir=manual_dir,
        url_infos=self.url_infos,
        manual_dir_instructions=self.MANUAL_DOWNLOAD_INSTRUCTIONS,
        force_download=(download_config.download_mode == FORCE_REDOWNLOAD),
        force_extraction=(download_config.download_mode == FORCE_REDOWNLOAD),
        force_checksums_validation=download_config.force_checksums_validation,
        register_checksums=download_config.register_checksums,
        register_checksums_path=register_checksums_path,
        verify_ssl=download_config.verify_ssl,
        dataset_name=self.name,
        max_simultaneous_downloads=max_simultaneous_downloads,
    )

  @utils.docs.do_not_doc_in_subclasses
  @utils.classproperty
  @classmethod
  def builder_config_cls(cls) -> Optional[type[BuilderConfig]]:
    """Returns the builder config class."""
    if not cls.BUILDER_CONFIGS:
      return None

    all_cls = {type(b) for b in cls.BUILDER_CONFIGS}
    if len(all_cls) != 1:
      raise ValueError(
          f"Could not infer the config class for {cls}. Detected: {all_cls}")

    (builder_cls,) = all_cls
    return builder_cls

  @property
  def builder_config(self) -> Optional[Any]:
    """`tfds.core.BuilderConfig` for this builder."""
    return self._builder_config

  def _create_builder_config(self, builder_config) -> Optional[BuilderConfig]:
    """Create and validate BuilderConfig object."""
    if builder_config is None:
      builder_config = self.get_default_builder_config()
      if builder_config is not None:
        logging.info("No config specified, defaulting to config: %s/%s",
                     self.name, builder_config.name)
    if not builder_config:
      return None
    if isinstance(builder_config, six.string_types):
      name = builder_config
      builder_config = self.builder_configs.get(name)
      if builder_config is None:
        raise ValueError("BuilderConfig %s not found. Available: %s" %
                         (name, list(self.builder_configs.keys())))
    name = builder_config.name
    if not name:
      raise ValueError("BuilderConfig must have a name, got %s" % name)
    is_custom = name not in self.builder_configs
    if is_custom:
      logging.warning("Using custom data configuration %s", name)
    else:
      if builder_config is not self.builder_configs[name]:
        raise ValueError(
            "Cannot name a custom BuilderConfig the same as an available "
            "BuilderConfig. Change the name. Available BuilderConfigs: %s" %
            (list(self.builder_configs.keys())))
    return builder_config

  @utils.classproperty
  @classmethod
  @utils.memoize()
  def builder_configs(cls):
    """Pre-defined list of configurations for this builder class."""
    config_dict = {config.name: config for config in cls.BUILDER_CONFIGS}
    if len(config_dict) != len(cls.BUILDER_CONFIGS):
      names = [config.name for config in cls.BUILDER_CONFIGS]
      raise ValueError(
          "Names in BUILDER_CONFIGS must not be duplicated. Got %s" % names)
    return config_dict


class FileReaderBuilder(DatasetBuilder):
  """Base class for datasets reading files.

  Subclasses are:

   * `GeneratorBasedBuilder`: Can both generate and read generated dataset.
   * `ReadOnlyBuilder`: Can only read pre-generated datasets. A user can
     generate a dataset with `GeneratorBasedBuilder`, and read them with
     `ReadOnlyBuilder` without requiring the original generation code.
  """

  @tfds_logging.builder_init()
  def __init__(
      self,
      *,
      file_format: Union[None, str, file_adapters.FileFormat] = None,
      **kwargs: Any,
  ):
    """Initializes an instance of FileReaderBuilder.

    Callers must pass arguments as keyword arguments.

    Args:
      file_format: EXPERIMENTAL, may change at any time; Format of the record
        files in which dataset will be read/written to. If `None`, defaults to
        `tfrecord`.
      **kwargs: Arguments passed to `DatasetBuilder`.
    """
    super().__init__(**kwargs)
    self.info.set_file_format(file_format)

  @utils.memoized_property
  def _example_specs(self):
    return self.info.features.get_serialized_info()

  def _as_dataset(  # pytype: disable=signature-mismatch  # overriding-parameter-type-checks
      self,
      split: splits_lib.Split,
      decoders: Optional[TreeDict[decode.partial_decode.DecoderArg]],
      read_config: read_config_lib.ReadConfig,
      shuffle_files: bool,
  ) -> tf.data.Dataset:
    # Partial decoding
    # TODO(epot): Should be moved inside `features.decode_example`
    if isinstance(decoders, decode.PartialDecoding):
      features = decoders.extract_features(self.info.features)
      example_specs = features.get_serialized_info()
      decoders = decoders.decoders
    # Full decoding (all features decoded)
    else:
      features = self.info.features
      example_specs = self._example_specs
      decoders = decoders  # pylint: disable=self-assigning-variable

    reader = reader_lib.Reader(
        self._data_dir,
        example_specs=example_specs,
        file_format=self.info.file_format,
    )
    decode_fn = functools.partial(features.decode_example, decoders=decoders)
    return reader.read(
        instructions=split,
        split_infos=self.info.splits.values(),
        decode_fn=decode_fn,
        read_config=read_config,
        shuffle_files=shuffle_files,
        disable_shuffling=self.info.disable_shuffling,
    )


class GeneratorBasedBuilder(FileReaderBuilder):
  """Base class for datasets with data generation based on file adapter.

  `GeneratorBasedBuilder` is a convenience class that abstracts away much
  of the data writing and reading of `DatasetBuilder`.

  It expects subclasses to overwrite `_split_generators` to return a dict of
  splits, generators. See the method docstrings for details.
  """

  @abc.abstractmethod
  @utils.docs.do_not_doc_in_subclasses
  @utils.docs.doc_private
  def _split_generators(
      self,
      dl_manager: download.DownloadManager,
  ) -> Dict[splits_lib.Split, split_builder_lib.SplitGenerator]:
    """Downloads the data and returns dataset splits with associated examples.

    Example:

    ```python
    def _split_generators(self, dl_manager):
      path = dl_manager.download_and_extract('http://dataset.org/my_data.zip')
      return {
          'train': self._generate_examples(path=path / 'train_imgs'),
          'test': self._generate_examples(path=path / 'test_imgs'),
      }
    ```

    * If the original dataset do not have predefined `train`, `test`,... splits,
      this function should only returns a single `train` split here. Users can
      use the [subsplit API](https://www.tensorflow.org/datasets/splits) to
      create subsplits (e.g.
      `tfds.load(..., split=['train[:75%]', 'train[75%:]'])`).
    * `tfds.download.DownloadManager` caches downloads, so calling `download`
      on the same url multiple times only download it once.
    * A good practice is to download all data in this function, and have all the
      computation inside `_generate_examples`.
    * Splits are generated in the order defined here. `builder.info.splits` keep
      the same order.
    * This function can have an extra `pipeline` kwarg only if some
      beam preprocessing should be shared across splits. In this case,
      a dict of `beam.PCollection` should be returned.
      See `_generate_example` for details.

    Args:
      dl_manager: `tfds.download.DownloadManager` used to download/extract the
        data

    Returns:
      The dict of split name, generators. See `_generate_examples` for details
      about the generator format.
    """
    raise NotImplementedError()

  @abc.abstractmethod
  @utils.docs.do_not_doc_in_subclasses
  @utils.docs.doc_private
  def _generate_examples(self,
                         **kwargs: Any) -> split_builder_lib.SplitGenerator:
    """Default function to generate examples for each split.

    The function should return a collection of `(key, examples)`. Examples
    will be encoded and written to disk. See `yields` section for details.

    The function can return/yield:

    * A python generator:

    ```python
    def _generate_examples(self, path):
      for filepath in path.iterdir():
        yield filepath.name, {'image': ..., 'label': ...}
    ```

    * A `beam.PTransform` of (input_types: [] -> output_types: `KeyExample`):
    For big datasets and distributed generation. See our Apache Beam
    [datasets guide](https://www.tensorflow.org/datasets/beam_datasets)
    for more info.

    ```python
    def _generate_examples(self, path):
      return (
          beam.Create(path.iterdir())
          | beam.Map(lambda filepath: filepath.name, {'image': ..., ...})
      )
    ```

    * A `beam.PCollection`: This should only be used if you need to share some
    distributed processing accross splits. In this case, you can use the
    following pattern:

    ```python
    def _split_generators(self, dl_manager, pipeline):
      ...
      # Distributed processing shared across splits
      pipeline |= beam.Create(path.iterdir())
      pipeline |= 'SharedPreprocessing' >> beam.Map(_common_processing)
      ...
      # Wrap the pipeline inside a ptransform_fn to add `'label' >> ` and avoid
      # duplicated PTransform nodes names.
      generate_examples = beam.ptransform_fn(self._generate_examples)
      return {
          'train': pipeline | 'train' >> generate_examples(is_train=True)
          'test': pipeline | 'test' >> generate_examples(is_train=False)
      }

    def _generate_examples(self, pipeline, is_train: bool):
      return pipeline | beam.Map(_split_specific_processing, is_train=is_train)
    ```

    Note: Each split should uses a different tag name (e.g.
    `'train' >> generate_examples(path)`). Otherwise Beam will raise
    duplicated name error.

    Args:
      **kwargs: Arguments from the `_split_generators`

    Yields:
      key: `str` or `int`, a unique deterministic example identification key.
        * Unique: An error will be raised if two examples are yield with the
          same key.
        * Deterministic: When generating the dataset twice, the same example
          should have the same key.
        * Comparable: If shuffling is disabled the key will be used to sort the
        dataset.
        Good keys can be the image id, or line number if examples are extracted
        from a text file.
        The example will be sorted by `hash(key)` if shuffling is enabled, and
        otherwise by `key`.
        Generating the dataset multiple times will keep examples in the
        same order.
      example: `dict<str feature_name, feature_value>`, a feature dictionary
        ready to be encoded and written to disk. The example will be
        encoded with `self.info.features.encode_example({...})`.
    """
    raise NotImplementedError()

  def _process_pipeline_result(
      self, pipeline_result: "runner.PipelineResult") -> None:
    """Processes the result of the beam pipeline if we used one.

    This can be used to (e.g) write beam counters to a file.
    Args:
      pipeline_result: PipelineResult returned by beam.Pipeline.run().
    """
    return

  def _download_and_prepare(  # pytype: disable=signature-mismatch  # overriding-parameter-type-checks
      self,
      dl_manager: download.DownloadManager,
      download_config: download.DownloadConfig,
  ) -> None:
    """Generate all splits and returns the computed split infos."""
    split_builder = split_builder_lib.SplitBuilder(
        split_dict=self.info.splits,
        features=self.info.features,
        dataset_size=self.info.dataset_size,
        max_examples_per_split=download_config.max_examples_per_split,
        beam_options=download_config.beam_options,
        beam_runner=download_config.beam_runner,
        file_format=self.info.file_format,
        shard_config=download_config.get_shard_config(),
    )
    # Wrap the generation inside a context manager.
    # If `beam` is used during generation (when a pipeline gets created),
    # the context manager is equivalent to `with beam.Pipeline()`.
    # Otherwise, this is a no-op.
    # By auto-detecting Beam, the user only has to change `_generate_examples`
    # to go from non-beam to beam dataset:
    # https://www.tensorflow.org/datasets/beam_datasets#instructions
    with split_builder.maybe_beam_pipeline() as maybe_pipeline_proxy:
      # If the signature has a `pipeline` kwargs, create the pipeline now and
      # forward it to `self._split_generators`
      # We add this magic because the pipeline kwargs is only used by c4 and
      # we do not want to make the API more verbose for a single advanced case.
      # See also the documentation at the end here:
      # https://www.tensorflow.org/datasets/api_docs/python/tfds/core/GeneratorBasedBuilder?version=nightly#_generate_examples
      signature = inspect.signature(self._split_generators)
      if "pipeline" in signature.parameters.keys():
        optional_pipeline_kwargs = dict(pipeline=split_builder.beam_pipeline)
      else:
        optional_pipeline_kwargs = {}
      split_generators = self._split_generators(  # pylint: disable=unexpected-keyword-arg
          dl_manager, **optional_pipeline_kwargs)
      # TODO(tfds): Could be removed once all datasets are migrated.
      # https://github.com/tensorflow/datasets/issues/2537
      # Legacy mode (eventually convert list[SplitGeneratorLegacy] -> dict)
      split_generators = split_builder.normalize_legacy_split_generators(
          split_generators=split_generators,
          generator_fn=self._generate_examples,
          is_beam=isinstance(self, BeamBasedBuilder),
      )

      # Ensure `all` isn't used as key.
      _check_split_names(split_generators.keys())

      # Writer fail if the number of example yield is `0`, so we return here.
      if download_config.max_examples_per_split == 0:
        return

      # Start generating data for all splits
      path_suffix = file_adapters.ADAPTER_FOR_FORMAT[
          self.info.file_format].FILE_SUFFIX

      split_info_futures = []
      for split_name, generator in utils.tqdm(
          split_generators.items(),
          desc="Generating splits...",
          unit=" splits",
          leave=False,
      ):
        filename_template = naming.ShardedFileTemplate(
            split=split_name,
            dataset_name=self.name,
            data_dir=self.data_path,
            filetype_suffix=path_suffix)
        future = split_builder.submit_split_generation(
            split_name=split_name,
            generator=generator,
            filename_template=filename_template,
            disable_shuffling=self.info.disable_shuffling,
        )
        split_info_futures.append(future)

    # Process the result of the beam pipeline.
    if maybe_pipeline_proxy._beam_pipeline:  # pylint:disable=protected-access
      self._process_pipeline_result(pipeline_result=maybe_pipeline_proxy.result)

    # Finalize the splits (after apache beam completed, if it was used)
    split_infos = [future.result() for future in split_info_futures]

    # Update the info object with the splits.
    split_dict = splits_lib.SplitDict(split_infos)
    self.info.set_splits(split_dict)


@utils.docs.deprecated
class BeamBasedBuilder(GeneratorBasedBuilder):
  """Beam based Builder.

  DEPRECATED: Please use `tfds.core.GeneratorBasedBuilder` instead.
  """

  def _generate_examples(self, *args: Any,
                         **kwargs: Any) -> split_builder_lib.SplitGenerator:
    return self._build_pcollection(*args, **kwargs)


def _check_split_names(split_names: Iterable[str]) -> None:
  """Check that split names are valid."""
  if "all" in set(str(s).lower() for s in split_names):
    raise ValueError(
        "`all` is a reserved keyword. Split cannot be named like this.")


def _get_default_config(
    builder_configs: List[BuilderConfig],
    default_config_name: Optional[str],
) -> Optional[BuilderConfig]:
  """Returns the default config from the given builder configs.

  Arguments:
    builder_configs: the configs from which the default should be picked.
    default_config_name: the name of the default config. If `None`, then it will
      use the first config.

  Returns:
    the default config. If there are no builder configs given, then None is
    returned.

  Raises:
    RuntimeError: if builder configs and a default config name are given, but no
    builder config with that name can be found.
  """
  if not builder_configs:
    return None
  if default_config_name is None:
    return builder_configs[0]
  for builder_config in builder_configs:
    if builder_config.name == default_config_name:
      return builder_config
  raise RuntimeError(
      f"Builder config with name `{default_config_name}` not found.")


def _save_default_config_name(
    common_dir: epath.Path,
    *,
    default_config_name: str,
) -> None:
  """Saves `builder_cls` metadata (common to all builder configs)."""
  data = {
      "default_config_name": default_config_name,
  }
  # `data_dir/ds_name/config/version/` -> `data_dir/ds_name/.config`
  config_dir = common_dir / ".config"
  config_dir.mkdir(parents=True, exist_ok=True)
  # Note:
  # * Save inside a dir to support some replicated filesystem
  # * Write inside a `.incomplete` file and rename to avoid multiple configs
  #   writing concurently the same file
  # * Config file is overwritten each time a config is generated. If the
  #   default config is changed, this will be updated.
  config_path = config_dir / "metadata.json"
  with utils.incomplete_file(config_path) as tmp_config_path:
    tmp_config_path.write_text(json.dumps(data))


def load_default_config_name(common_dir: epath.Path,) -> Optional[str]:
  """Load `builder_cls` metadata (common to all builder configs)."""
  config_path = epath.Path(common_dir) / ".config/metadata.json"
  if not config_path.exists():
    return None
  data = json.loads(config_path.read_text())
  return data.get("default_config_name")


def cannonical_version_for_config(
    instance_or_cls: Union[DatasetBuilder, Type[DatasetBuilder]],
    config: Optional[BuilderConfig] = None,
) -> utils.Version:
  """Get the cannonical version for the given config.

  This allow to get the version without instanciating the class.
  The version can be stored either at the class or in the config object.

  Args:
    instance_or_cls: The instance or class on which get the version
    config: The config which might contain the version, or None if the dataset
      do not have config.

  Returns:
    version: The extracted version.
  """
  if instance_or_cls.BUILDER_CONFIGS and config is None:
    raise ValueError(
        f"Cannot infer version on {instance_or_cls.name}. Unknown config.")

  if config and config.version:
    return utils.Version(config.version)
  elif instance_or_cls.VERSION:
    return utils.Version(instance_or_cls.VERSION)
  else:
    raise ValueError(
        f"DatasetBuilder {instance_or_cls.name} does not have a defined "
        "version. Please add a `VERSION = tfds.core.Version('x.y.z')` to the "
        "class.")
