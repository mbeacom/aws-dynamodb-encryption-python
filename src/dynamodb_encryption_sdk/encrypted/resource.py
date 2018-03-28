# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
""""""
import attr
from boto3.resources.base import ServiceResource
from boto3.resources.collection import CollectionManager

from . import CryptoConfig, validate_get_arguments
from .item import decrypt_python_item, encrypt_python_item
from .table import EncryptedTable
from dynamodb_encryption_sdk.internal.utils import TableInfoCache
from dynamodb_encryption_sdk.material_providers import CryptographicMaterialsProvider
from dynamodb_encryption_sdk.structures import AttributeActions, EncryptionContext

__all__ = ('EncryptedResource',)


@attr.s
class EncryptedTablesCollectionManager(object):
    """Tables collection manager that provides EncryptedTable objects.

    https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.tables

    :param collection: Pre-configured boto3 DynamoDB table collection manager
    :type collection: boto3.resources.collection.CollectionManager
    :param materials_provider: Cryptographic materials provider to use
    :type materials_provider: dynamodb_encryption_sdk.material_providers.CryptographicMaterialsProvider
    :param attribute_actions: Table-level configuration of how to encrypt/sign attributes
    :type attribute_actions: dynamodb_encryption_sdk.structures.AttributeActions
    :param table_info_cache: Local cache from which to obtain TableInfo data
    :type table_info_cache: dynamodb_encryption_sdk.internal.utils.TableInfoCache
    """
    _collection = attr.ib(validator=attr.validators.instance_of(CollectionManager))
    _materials_provider = attr.ib(validator=attr.validators.instance_of(CryptographicMaterialsProvider))
    _attribute_actions = attr.ib(validator=attr.validators.instance_of(AttributeActions))
    _table_info_cache = attr.ib(validator=attr.validators.instance_of(TableInfoCache))

    def __getattr__(self, name):
        """Catch any method/attribute lookups that are not defined in this class and try
        to find them on the provided collection object.

        :param str name: Attribute name
        :returns: Result of asking the provided collection object for that attribute name
        :raises AttributeError: if attribute is not found on provided collection object
        """
        return getattr(self._collection, name)

    def _transform_table(self, method, **kwargs):
        """Transform a Table from the underlying collection manager to an EncryptedTable.

        :param method: Method on underlying collection manager to call
        :type method: callable
        :param **kwargs: Keyword arguments to pass to ``method``
        """
        for table in method(**kwargs):
            yield EncryptedTable(
                table=table,
                materials_provider=self._materials_provider,
                table_info=self._table_info_cache.table_info(table.name),
                attribute_actions=self._attribute_actions
            )

    def all(self):
        """Creates an iterable of all EncryptedTable resources in the collection.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.all
        """
        return self._transform_table(self._collection.all)

    def filter(self, **kwargs):
        """Creates an iterable of all EncryptedTable resources in the collection filtered by kwargs passed to method.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.filter
        """
        return self._transform_table(self._collection.filter, **kwargs)

    def limit(self, **kwargs):
        """Creates an iterable up to a specified amount of EncryptedTable resources in the collection.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.limit
        """
        return self._transform_table(self._collection.limit, **kwargs)

    def page_size(self, **kwargs):
        """Creates an iterable of all EncryptedTable resources in the collection, but limits
        the number of items returned by each service call by the specified amount.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.page_size
        """
        return self._transform_table(self._collection.page_size, **kwargs)


@attr.s
class EncryptedResource(object):
    """High-level helper class to provide a familiar interface to encrypted tables.

    .. note::

        This class provides a superset of the boto3 DynamoDB service resource API, so should
        work as a drop-in replacement once configured.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#service-resource

        If you want to provide per-request cryptographic details, the ``batch_write_item``
        and ``batch_get_item`` methods will also accept a ``crypto_config`` parameter, defining
        a custom ``CryptoConfig`` instance for this request.

    :param resource: Pre-configured boto3 DynamoDB service resource object
    :type resource: boto3.resources.base.ServiceResource
    :param materials_provider: Cryptographic materials provider to use
    :type materials_provider: dynamodb_encryption_sdk.material_providers.CryptographicMaterialsProvider
    :param attribute_actions: Table-level configuration of how to encrypt/sign attributes
    :type attribute_actions: dynamodb_encryption_sdk.structures.AttributeActions
    :param bool auto_refresh_table_indexes: Should we attempt to refresh information about table indexes?
        Requires ``dynamodb:DescribeTable`` permissions on each table. (default: True)
    """
    _resource = attr.ib(validator=attr.validators.instance_of(ServiceResource))
    _materials_provider = attr.ib(validator=attr.validators.instance_of(CryptographicMaterialsProvider))
    _attribute_actions = attr.ib(
        validator=attr.validators.instance_of(AttributeActions),
        default=attr.Factory(AttributeActions)
    )
    _auto_refresh_table_indexes = attr.ib(
        validator=attr.validators.instance_of(bool),
        default=True
    )

    def __attrs_post_init__(self):
        """Set up the table info cache and the encrypted tables collection manager."""
        self._table_info_cache = TableInfoCache(
            client=self._resource.meta.client,
            auto_refresh_table_indexes=self._auto_refresh_table_indexes
        )
        self.tables = EncryptedTablesCollectionManager(
            collection=self._resource.tables,
            materials_provider=self._materials_provider,
            attribute_actions=self._attribute_actions,
            table_info_cache=self._table_info_cache
        )

    def __getattr__(self, name):
        """Catch any method/attribute lookups that are not defined in this class and try
        to find them on the provided resource object.

        :param str name: Attribute name
        :returns: Result of asking the provided resource object for that attribute name
        :raises AttributeError: if attribute is not found on provided resource object
        """
        return getattr(self._resource, name)

    def _crypto_config(self, table_name):
        """Pull all encryption-specific parameters from the request and use them to build a crypto config.

        :returns: crypto config
        :rtype: dynamodb_encryption_sdk.encrypted.CryptoConfig
        """
        table_info = self._table_info_cache.table_info(table_name)

        attribute_actions = self._attribute_actions.copy()
        attribute_actions.set_index_keys(*table_info.protected_index_keys())

        crypto_config = CryptoConfig(
            materials_provider=self._materials_provider,
            encryption_context=EncryptionContext(**table_info.encryption_context_values),
            attribute_actions=attribute_actions
        )
        return crypto_config

    def batch_get_item(self, **kwargs):
        """Transparently decrypt multiple items after getting them from a batch get item request.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.batch_get_item
        """
        request_crypto_config = kwargs.pop('crypto_config', None)

        for _table_name, table_kwargs in kwargs['RequestItems'].items():
            validate_get_arguments(table_kwargs)

        response = self._resource.batch_get_item(**kwargs)
        for table_name, items in response['Responses'].items():
            if request_crypto_config is not None:
                crypto_config = request_crypto_config
            else:
                crypto_config = self._crypto_config(table_name)

            for pos in range(len(items)):
                items[pos] = decrypt_python_item(
                    item=items[pos],
                    crypto_config=crypto_config
                )
        return response

    def batch_write_item(self, **kwargs):
        """Transparently encrypt multiple items before writing them with a batch write item request.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.batch_write_item
        """
        request_crypto_config = kwargs.pop('crypto_config', None)

        for table_name, items in kwargs['RequestItems'].items():
            if request_crypto_config is not None:
                crypto_config = request_crypto_config
            else:
                crypto_config = self._crypto_config(table_name)

            for pos in range(len(items)):
                for request_type, item in items[pos].items():
                    # We don't encrypt primary indexes, so we can ignore DeleteItem requests
                    if request_type == 'PutRequest':
                        items[pos][request_type]['Item'] = encrypt_python_item(
                            item=item['Item'],
                            crypto_config=crypto_config
                        )
        return self._resource.batch_write_item(**kwargs)

    def Table(self, name, **kwargs):
        """Creates an EncryptedTable resource.

        If any of the optional configuration values are not provided, the corresponding values
        for this ``EncryptedResource`` will be used.

        https://boto3.readthedocs.io/en/latest/reference/services/dynamodb.html#DynamoDB.ServiceResource.Table

        :param name: The table name.
        :param materials_provider: Cryptographic materials provider to use (optional)
        :type materials_provider: dynamodb_encryption_sdk.material_providers.CryptographicMaterialsProvider
        :param table_info: Information about the target DynamoDB table (optional)
        :type table_info: dynamodb_encryption_sdk.structures.TableInfo
        :param attribute_actions: Table-level configuration of how to encrypt/sign attributes (optional)
        :type attribute_actions: dynamodb_encryption_sdk.structures.AttributeActions
        """
        table_kwargs = dict(
            table=self._resource.Table(name),
            materials_provider=kwargs.get('materials_provider', self._materials_provider),
            attribute_actions=kwargs.get('attribute_actions', self._attribute_actions),
            auto_refresh_table_indexes=kwargs.get('auto_refresh_table_indexes', self._auto_refresh_table_indexes),
            table_info=self._table_info_cache.table_info(name)
        )

        return EncryptedTable(**table_kwargs)