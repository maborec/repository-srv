# -*- coding: utf-8 -*-
# Copyright 2016 Open Permissions Platform Coalition
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed under the License is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.


import re
import json
import uuid
import logging
import urllib

import rdflib.graph

from chub import API
from chub.oauth2 import Write, get_token
from koi.configure import ssl_server_options
from functools import partial
from tornado.gen import coroutine, Return
from tornado.ioloop import IOLoop
from tornado.options import options

from .queries.asset import (ASSET_CLASS,
                            ASSET_APPEND_ALSO_IDENTIFIED,
                            ASSET_QUERY_ALL_ENTITY_IDS,
                            ASSET_LIST_EXTRA_IDS,
                            ASSET_LIST_EXTRA_QUERY,
                            ASSET_GET_ALSO_IDENTIFIED,
                            ASSET_SELECT_BY_SOURCE_ID,
                            ASSET_SELECT_BY_ENTITY_ID,
                            ASSET_STRUCT_SELECT
                            )
from .queries.generic import *
from .framework.entity import Entity, build_sparql_str, build_uuid_reference, build_hub_reference, ENTITY_ID_REGEX


TYPE_MAPPING = {
    'text/rdf+n3': 'n3',
    'application/xml': 'xml'
}


@coroutine
def _insert_ids(repository, entity_id, ids):
    """
    Add some asset ids to an existing Asset
    :param repository_id: id of repository/namespace
    :param entity_id: the id of the asset
    :param ids: ids to be added
    """
    if not ids:
        raise Return()

    query = ""
    for next_id in ids:
        query += ASSET_APPEND_ALSO_IDENTIFIED.format(
            entity_id=Asset.normalise_id(entity_id),
            source_id_type=build_hub_reference(next_id['source_id_type']),
            source_id_value=build_sparql_str(next_id['source_id']),
            bnodeuuid0=str(uuid.uuid4()).replace('-', '')
        )
    query = SPARQL_PREFIXES + INSERT_DATA % (query,)

    yield repository.update(query, response_type='csv')

    raise Return()


def _validate_ids(ids):
    """
    Check list of ids dictionaries for 'source_id' and 'source_id_type' keys
    :param ids: list of id dictionaries
    :return: a list of errors
    """
    errors = []
    for index, data_id in enumerate(ids):
        if not data_id.get('source_id_type'):
            errors.append('Missing source_id_type for entry:{}'.format(index + 1))
        if not data_id.get('source_id'):
            errors.append('Missing source_id for entry:{}'.format(index + 1))
    return errors


def get_asset_ids(assets_data, content_type):
    """
    This function extracts source_id, source_id_type and entity_uri of each asset in the data.
    It does this by creating an in memory graph via RDF Lib.

    * Caution this is potentially very cpu expensive and could use a lot of memory

    :param assets_data: A blob of triples
    :param content_type:  The type of the blob
    :return: an array of dicts that show the ids of assets
    """
    graph = rdflib.graph.Graph()
    graph.parse(data=assets_data, format=TYPE_MAPPING[content_type])
    result = graph.query(SPARQL_PREFIXES + ASSET_QUERY_ALL_ENTITY_IDS)
    result = [{u'entity_id': x[u'entity_id'].split('/')[-1]} for x in result]
    return result


@coroutine
def add_ids(repository, entity_id, ids):
    """
    Add ids to an asset
    :param repository:  repository/namespace
    :param entity_id: the hub key for the asset
    :param ids: the new ids
    :return: a list of errors
    """
    errors = _validate_ids(ids)
    if ids and not errors:
        yield _insert_ids(repository, entity_id, ids)
        yield insert_timestamps(repository, [entity_id])

    raise Return(errors)


@coroutine
def send_notification(repository, **kwargs):
    """
    Send a fire-and-forget notification to the index service

    NOTE: all exceptions are unhandled. It's assumed that the function is used
    as a callback outside of the request's context using IOLoop.spawn_callback
    (see: http://www.tornadoweb.org/en/stable/ioloop.html)
    """
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }

    try:
        token = yield get_token(options.url_auth,
                                options.service_id,
                                options.client_secret,
                                scope=Write(options.url_index),
                                ssl_options=ssl_server_options())
        client = API(options.url_index,
                     token=token,
                     ssl_options=ssl_server_options())
        client.index.notifications.prepare_request(
            request_timeout=options.request_timeout,
            headers=headers,
            body=json.dumps({'id': repository.repository_id}))

        yield client.index.notifications.post()
    except Exception as e:
        logging.exception("failed to notify index: " + e.message)


## ASSET MODEL
class Asset(Entity):
    CLASS = ASSET_CLASS
    STRUCT_SELECT = ASSET_STRUCT_SELECT

    @classmethod
    def normalise_id(cls, entity_id):
        """
        Ensure asset ids have the right form.

        :param entity_id: The entity_id of the offer either in the form of an IRI, either as an id
        :returns: an id normalised usable in SPARQL queries
        """
        if entity_id.startswith(PREFIXES['id']):
            entity_id = entity_id[len(PREFIXES['id']):]
        entity_id = entity_id.lower()
        if not re.match("id:{}".format(ENTITY_ID_REGEX), entity_id):
            return build_uuid_reference(entity_id)
        return entity_id

    # LIST VIEW
    LIST_EXTRA_IDS = ASSET_LIST_EXTRA_IDS
    LIST_EXTRA_QUERY = ASSET_LIST_EXTRA_QUERY

    @classmethod
    @coroutine
    def get_also_identified_by(cls, cdb, entity_id):
        """
        Attach license objects to asset data
        :param cdb: db containing the licenses
        :param entity_id: the asset being queried
        :return: a list of source_id and source_id_type
        """

        rsp = yield cdb.query(
            SPARQL_PREFIXES + (
              ASSET_GET_ALSO_IDENTIFIED.format(
                entity_id=cls.normalise_id(entity_id)
              )
            )
        )

        result = cls._parse_response(rsp)

        raise Return([{'source_id_type': x[0].split('/')[-1], 'source_id': x[1]} for x in result])

    @classmethod
    def asset_subselect_idlist(cls, idlist, idname="entity"):
        """
        Returns a query to resolve assets selected by the idlist.

        :param idname: The name of the id
        :returns: an id normalised usable in SPARQL queries
        """
        subqueries = []
        if not len(idlist):
            raise ValueError("Invalid asset list")
        hub_keys = filter(lambda id: id["source_id_type"] == "hub_key", idlist)
        otherids = filter(lambda id: id["source_id_type"] != "hub_key", idlist)
        if len(hub_keys) > 0:
            idlist = ["(%s)" % (cls.normalise_id(hk['source_id'])) for hk in hub_keys]
            subqueries.append(
                "{\n%s}\n" % (ASSET_SELECT_BY_ENTITY_ID.format(idname=idname, idlist="\n".join(idlist)),)
            )
        if len(otherids) > 0:
            idlist = ["(%s %s)" % (build_hub_reference(urllib.quote_plus(i['source_id_type'])),
                                   build_sparql_str(urllib.quote_plus(i['source_id'])))
                      for i in otherids]
            subqueries.append(
                "{\n%s}\n" %(ASSET_SELECT_BY_SOURCE_ID.format(idname=idname, idlist="\n".join(idlist)),)
            )

        return "{\n%s}\n" % (" UNION ".join(subqueries))

    @classmethod
    @coroutine
    def on_store(cls, payload, content_type, repository):
        assetids = get_asset_ids(payload, content_type)
        entity_ids = [x[u'entity_id'] for x in assetids]
        yield Entity.insert_timestamps(ids=entity_ids, repository=repository)

        # Send notification to index service if not in standalone mode
        if not options.standalone:
            IOLoop.current().spawn_callback(partial(eval('send_notification'), repository))

        raise Return(assetids)


@coroutine
def retrieve_paged_assets(repository, from_time, to_time, page=1, page_size=1000):
    """
    Get identifiers of assets that have been modified/added within time window.

    :param from_time: datetime
    :param to_time: datetime
    :param repository: the repository/namespace that we want to query
    :param page: query offset
    :param page_size:
    """
    where = Asset.timerange_constraint(
        "when",
        from_time=from_time.isoformat(),
        to_time=to_time.isoformat())

    query_result = yield Asset._retrieve_paged_ids(where, page=page, page_size=page_size, repository=repository)

    # todo: clarify/comment what this is up to
    fields = ['entity_uri',  'source_id', 'source_id_type', 'last_modified']
    results = [dict(zip(fields, x)) for x in query_result]

    if not results:
        raise Return(([], ()))

    for i in range(len(results)):
        results[i]['entity_id'] = results[i]['entity_uri'].split('/')[-1]
        results[i]['source_id_type'] = results[i]['source_id_type'].split('/')[-1]

    if 'last_modified' not in results[0]:
        logging.warning("'last_modified' not present: invalid query - check named queries: %r"%(results[0].items(),))

    results_range = (results[0]['last_modified'], results[-1]['last_modified'])
    raise Return((results, results_range))


exists = Asset.exists
insert_timestamps = Asset.insert_timestamps
store = Asset.store
