from __future__ import absolute_import

import json

from six.moves.urllib.parse import urlparse

from django.db import models
from django.db.models.sql.where import SubqueryConstraint

from elasticsearch import Elasticsearch, NotFoundError, RequestError
from elasticsearch.helpers import bulk

from wagtail.wagtailsearch.backends.base import BaseSearch
from wagtail.wagtailsearch.indexed import Indexed, SearchField, FilterField


class ElasticSearchMapping(object):
    TYPE_MAP = {
        'AutoField': 'integer',
        'BinaryField': 'binary',
        'BooleanField': 'boolean',
        'CharField': 'string',
        'CommaSeparatedIntegerField': 'string',
        'DateField': 'date',
        'DateTimeField': 'date',
        'DecimalField': 'double',
        'FileField': 'string',
        'FilePathField': 'string',
        'FloatField': 'double',
        'IntegerField': 'integer',
        'BigIntegerField': 'long',
        'IPAddressField': 'string',
        'GenericIPAddressField': 'string',
        'NullBooleanField': 'boolean',
        'OneToOneField': 'integer',
        'PositiveIntegerField': 'integer',
        'PositiveSmallIntegerField': 'integer',
        'SlugField': 'string',
        'SmallIntegerField': 'integer',
        'TextField': 'string',
        'TimeField': 'date',
    }

    def __init__(self, model):
        self.model = model

    def get_document_type(self):
        return self.model.indexed_get_content_type()

    def get_field_mapping(self, field):
        mapping = {'type': self.TYPE_MAP.get(field.get_type(self.model), 'string')}

        if isinstance(field, SearchField):
            if field.boost:
                mapping['boost'] = field.boost

            if field.partial_match:
                mapping['analyzer'] = 'edgengram_analyzer'

            mapping['include_in_all'] = True
        elif isinstance(field, FilterField):
            mapping['index'] = 'not_analyzed'
            mapping['include_in_all'] = False

        if 'es_extra' in field.kwargs:
            for key, value in field.kwargs['es_extra'].items():
                mapping[key] = value

        return field.get_index_name(self.model), mapping

    def get_mapping(self):
        # Make field list
        fields = {
            'pk': dict(type='string', index='not_analyzed', store='yes', include_in_all=False),
            'content_type': dict(type='string', index='not_analyzed', include_in_all=False),
            '_partials': dict(type='string', analyzer='edgengram_analyzer', include_in_all=False),
        }

        fields.update(dict(
            self.get_field_mapping(field) for field in self.model.get_search_fields()
        ))

        return {
            self.get_document_type(): {
                'properties': fields,
            }
        }

    def get_document_id(self, obj):
        return obj.indexed_get_toplevel_content_type() + ':' + str(obj.pk)

    def get_document(self, obj):
        # Build document
        doc = dict(pk=str(obj.pk), content_type=self.model.indexed_get_content_type())
        partials = []
        for field in self.model.get_search_fields():
            value = field.get_value(obj)

            doc[field.get_index_name(self.model)] = value

            # Check if this field should be added into _partials
            if isinstance(field, SearchField) and field.partial_match:
                partials.append(value)

        # Add partials to document
        doc['_partials'] = partials

        return doc

    def __repr__(self):
        return '<ElasticSearchMapping: %s>' % (self.model.__name__, )


class FilterError(Exception):
    pass


class FieldError(Exception):
    pass


class ElasticSearchQuery(object):
    def __init__(self, queryset, query_string, fields=None):
        self.queryset = queryset
        self.query_string = query_string
        self.fields = fields

    def _get_filters_from_where(self, where_node):
        # Check if this is a leaf node
        if isinstance(where_node, tuple):
            field_name = where_node[0].col
            lookup = where_node[1]
            value = where_node[3]

            # Get field
            field = dict(
                (field.get_attname(self.queryset.model), field)
                for field in self.queryset.model.get_filterable_search_fields()
            ).get(field_name, None)

            # Give error if the field doesn't exist
            if field is None:
                raise FieldError('Cannot filter ElasticSearch results with field "' + field_name + '". Please add FilterField(\'' + field_name + '\') to ' + self.queryset.model.__name__ + '.search_fields.')

            # Get the name of the field in the index
            field_index_name = field.get_index_name(self.queryset.model)

            # Find lookup
            if lookup == 'exact':
                if value is None:
                    return {
                        'missing': {
                            'field': field_index_name,
                        }
                    }
                else:
                    return {
                        'term': {
                            field_index_name: value,
                        }
                    }

            if lookup == 'isnull':
                if value:
                    return {
                        'missing': {
                            'field': field_index_name,
                        }
                    }
                else:
                    return {
                        'not': {
                            'missing': {
                                'field': field_index_name,
                            }
                        }
                    }

            if lookup in ['startswith', 'prefix']:
                return {
                    'prefix': {
                        field_index_name: value,
                    }
                }

            if lookup in ['gt', 'gte', 'lt', 'lte']:
                return {
                    'range': {
                        field_index_name: {
                            lookup: value,
                        }
                    }
                }

            if lookup == 'range':
                lower, upper = value

                return {
                    'range': {
                        field_index_name: {
                            'gte': lower,
                            'lte': upper,
                        }
                    }
                }

            if lookup == 'in':
                return {
                    'terms': {
                        field_index_name: value,
                    }
                }

            raise FilterError('Could not apply filter on ElasticSearch results: "' + field_name + '__' + lookup + ' = ' + unicode(value) + '". Lookup "' + lookup + '"" not recognosed.')
        elif isinstance(where_node, SubqueryConstraint):
            raise FilterError('Could not apply filter on ElasticSearch results: Subqueries are not allowed.')

        # Get child filters
        connector = where_node.connector
        child_filters = [self._get_filters_from_where(child) for child in where_node.children]
        child_filters = [child_filter for child_filter in child_filters if child_filter]

        # Connect them
        if child_filters:
            if len(child_filters) == 1:
                filter_out = child_filters[0]
            else:
                filter_out = {
                    connector.lower(): [
                        fil for fil in child_filters if fil is not None
                    ]
                }

            if where_node.negated:
                filter_out = {
                    'not': filter_out
                }

            return filter_out

    def _get_filters(self):
        # Filters
        filters = []

        # Filter by content type
        filters.append({
            'prefix': {
                'content_type': self.queryset.model.indexed_get_content_type()
            }
        })

        # Apply filters from queryset
        queryset_filters = self._get_filters_from_where(self.queryset.query.where)
        if queryset_filters:
            filters.append(queryset_filters)

        return filters

    def to_es(self):
        # Query
        if self.query_string is not None:
            fields = self.fields or ['_all', '_partials']

            if len(fields) == 1:
                query = {
                    'match': {
                        fields[0]: self.query_string,
                    }
                }
            else:
                query = {
                    'multi_match': {
                        'query': self.query_string,
                        'fields': fields,
                    }
                }
        else:
            query = {
                'match_all': {}
            }

        # Filters
        filters = self._get_filters()
        if len(filters) == 1:
            query = {
                'filtered': {
                    'query': query,
                    'filter': filters[0],
                }
            }
        elif len(filters) > 1:
            query = {
                'filtered': {
                    'query': query,
                    'filter': {
                        'and': filters,
                    }
                }
            }

        return query

    def __repr__(self):
        return json.dumps(self.to_es())


class ElasticSearchResults(object):
    def __init__(self, backend, query, prefetch_related=None):
        self.backend = backend
        self.query = query
        self.prefetch_related = prefetch_related
        self.start = 0
        self.stop = None
        self._results_cache = None
        self._count_cache = None

    def _set_limits(self, start=None, stop=None):
        if stop is not None:
            if self.stop is not None:
                self.stop = min(self.stop, self.start + stop)
            else:
                self.stop = self.start + stop

        if start is not None:
            if self.stop is not None:
                self.start = min(self.stop, self.start + start)
            else:
                self.start = self.start + start

    def _clone(self):
        klass = self.__class__
        new = klass(self.backend, self.query, prefetch_related=self.prefetch_related)
        new.start = self.start
        new.stop = self.stop
        return new

    def _do_search(self):
        # Params for elasticsearch query
        params = dict(
            index=self.backend.es_index,
            body=dict(query=self.query.to_es()),
            _source=False,
            fields='pk',
            from_=self.start,
        )

        # Add size if set
        if self.stop is not None:
            params['size'] = self.stop - self.start

        # Send to ElasticSearch
        hits = self.backend.es.search(**params)

        # Get pks from results
        pks = [hit['fields']['pk'] for hit in hits['hits']['hits']]

        # ElasticSearch 1.x likes to pack pks into lists, unpack them if this has happened
        pks = [pk[0] if isinstance(pk, list) else pk for pk in pks]

        # Initialise results dictionary
        results = dict((str(pk), None) for pk in pks)

        # Find objects in database and add them to dict
        queryset = self.query.queryset.filter(pk__in=pks)
        for obj in queryset:
            results[str(obj.pk)] = obj

        # Return results in order given by ElasticSearch
        return [results[str(pk)] for pk in pks if results[str(pk)]]

    def _do_count(self):
        # Get query
        query = self.query.to_es()

        # Elasticsearch 1.x
        count = self.backend.es.count(
            index=self.backend.es_index,
            body=dict(query=query),
        )

        # ElasticSearch 0.90.x fallback
        if not count['_shards']['successful'] and "No query registered for [query]]" in count['_shards']['failures'][0]['reason']:
            count = self.backend.es.count(
                index=self.backend.es_index,
                body=query,
            )

        # Get count
        hit_count = count['count']

        # Add limits
        hit_count -= self.start
        if self.stop is not None:
            hit_count = min(hit_count, self.stop - self.start)

        return max(hit_count, 0)

    def results(self):
        if self._results_cache is None:
            self._results_cache = self._do_search()
        return self._results_cache

    def count(self):
        if self._count_cache is None:
            if self._results_cache is not None:
                self._count_cache = len(self._results_cache)
            else:
                self._count_cache = self._do_count()
        return self._count_cache

    def __getitem__(self, key):
        new = self._clone()

        if isinstance(key, slice):
            # Set limits
            start = int(key.start) if key.start else None
            stop = int(key.stop) if key.stop else None
            new._set_limits(start, stop)

            # Copy results cache
            if self._results_cache is not None:
                new._results_cache = self._results_cache[key]

            return new
        else:
            if self._results_cache is not None:
                return self._results_cache[key]

            new.start = key
            new.stop = key + 1
            return list(new)[0]

    def __iter__(self):
        return iter(self.results())

    def __len__(self):
        return len(self.results())

    def __repr__(self):
        data = list(self[:21])
        if len(data) > 20:
            data[-1] = "...(remaining elements truncated)..."
        return repr(data)


class ElasticSearch(BaseSearch):
    def __init__(self, params):
        super(ElasticSearch, self).__init__(params)

        # Get settings
        self.es_hosts = params.pop('HOSTS', None)
        self.es_urls = params.pop('URLS', ['http://localhost:9200'])
        self.es_index = params.pop('INDEX', 'wagtail')
        self.es_timeout = params.pop('TIMEOUT', 10)

        # If HOSTS is not set, convert URLS setting to HOSTS
        if self.es_hosts is None:
            self.es_hosts = []

            for url in self.es_urls:
                parsed_url = urlparse(url)

                self.es_hosts.append({
                    'host': parsed_url.hostname,
                    'port': parsed_url.port or 9200,
                    'url_prefix': parsed_url.path,
                    'use_ssl': parsed_url.scheme == 'https',
                })

        # Get ElasticSearch interface
        # Any remaining params are passed into the ElasticSearch constructor
        self.es = Elasticsearch(
            hosts=self.es_hosts,
            timeout=self.es_timeout,
            **params)

    def reset_index(self):
        # Delete old index
        try:
            self.es.indices.delete(self.es_index)
        except NotFoundError:
            pass

        # Settings
        INDEX_SETTINGS = {
            'settings': {
                'analysis': {
                    'analyzer': {
                        'ngram_analyzer': {
                            'type': 'custom',
                            'tokenizer': 'lowercase',
                            'filter': ['ngram']
                        },
                        'edgengram_analyzer': {
                            'type': 'custom',
                            'tokenizer': 'lowercase',
                            'filter': ['edgengram']
                        }
                    },
                    'tokenizer': {
                        'ngram_tokenizer': {
                            'type': 'nGram',
                            'min_gram': 3,
                            'max_gram': 15,
                        },
                        'edgengram_tokenizer': {
                            'type': 'edgeNGram',
                            'min_gram': 2,
                            'max_gram': 15,
                            'side': 'front'
                        }
                    },
                    'filter': {
                        'ngram': {
                            'type': 'nGram',
                            'min_gram': 3,
                            'max_gram': 15
                        },
                        'edgengram': {
                            'type': 'edgeNGram',
                            'min_gram': 1,
                            'max_gram': 15
                        }
                    }
                }
            }
        }

        # Create new index
        self.es.indices.create(self.es_index, INDEX_SETTINGS)

    def add_type(self, model):
        # Get mapping
        mapping = ElasticSearchMapping(model)

        # Put mapping
        self.es.indices.put_mapping(index=self.es_index, doc_type=mapping.get_document_type(), body=mapping.get_mapping())

    def refresh_index(self):
        self.es.indices.refresh(self.es_index)

    def add(self, obj):
        # Make sure the object can be indexed
        if not self.object_can_be_indexed(obj):
            return

        # Get mapping
        mapping = ElasticSearchMapping(obj.__class__)

        # Add document to index
        self.es.index(self.es_index, mapping.get_document_type(), mapping.get_document(obj), id=mapping.get_document_id(obj))

    def add_bulk(self, obj_list):
        # Group all objects by their type
        type_set = {}
        for obj in obj_list:
            # Object must be a decendant of Indexed and be a django model
            if not self.object_can_be_indexed(obj):
                continue

            # Get mapping
            mapping = ElasticSearchMapping(obj.__class__)

            # Get document type
            doc_type = mapping.get_document_type()

            # If type is currently not in set, add it
            if doc_type not in type_set:
                type_set[doc_type] = []

            # Add document to set
            type_set[doc_type].append((mapping.get_document_id(obj), mapping.get_document(obj)))

        # Loop through each type and bulk add them
        for type_name, type_documents in type_set.items():
            # Get list of actions
            actions = []
            for doc_id, doc in type_documents:
                action = {
                    '_index': self.es_index,
                    '_type': type_name,
                    '_id': doc_id,
                }
                action.update(doc)
                actions.append(action)

            yield type_name, len(type_documents)
            bulk(self.es, actions)

    def delete(self, obj):
        # Object must be a decendant of Indexed and be a django model
        if not isinstance(obj, Indexed) or not isinstance(obj, models.Model):
            return

        # Get mapping
        mapping = ElasticSearchMapping(obj.__class__)

        # Delete document
        try:
            self.es.delete(
                self.es_index,
                mapping.get_document_type(),
                mapping.get_document_id(obj),
            )
        except NotFoundError:
            pass  # Document doesn't exist, ignore this exception

    def _search(self, queryset, query_string, fields=None):
        return ElasticSearchResults(self, ElasticSearchQuery(queryset, query_string, fields=fields))
