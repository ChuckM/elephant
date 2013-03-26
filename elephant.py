# -*- coding: utf-8 -*-

import os
import json
import time
import urlparse
from datetime import datetime
from uuid import uuid4

import boto
import requests
from flask import Flask, request, Response, jsonify, redirect
from flask.ext.script import Manager
from clint.textui import progress
from pyelasticsearch import ElasticSearch
from pyelasticsearch.exceptions import IndexAlreadyExistsError


app = Flask(__name__)
manager = Manager(app)

# Configuration
app.debug = 'DEBUG' in os.environ

# The Elastic Search endpoint to use.
ELASTICSEARCH_URL = os.environ['ELASTICSEARCH_URL']
CLUSTER_NAME = os.environ['CLUSTER_NAME']
API_KEY = os.environ['API_KEY']

# If S3 bucket doesn't exist, set it up.
BUCKET_NAME = 'elephant-{}'.format(CLUSTER_NAME)
BUCKET = boto.connect_s3().create_bucket(BUCKET_NAME)

# Elastic Search Stuff.
ES = ElasticSearch(ELASTICSEARCH_URL)
_url = urlparse.urlparse(ES.servers.live[0])
ES_AUTH = (_url.username, _url.password)


def epoch(dt=None):
    """Returns the epoch value for the given datetime, defulting to now."""

    if not dt:
        dt = datetime.utcnow()

    return int(time.mktime(dt.timetuple()) * 1000 + dt.microsecond / 1000)


class Collection(object):
    """A set of Records."""

    def __init__(self, name):
        self.name = name

    def __getitem__(self, k):
        return Record._from_uuid(k, collection=self.name)

    def iter_search(self, query, **kwargs):
        """Returns an iterator of Records for the given query."""

        if query is None:
            query = '*'

        # Prepare elastic search queries.
        params = {}
        for (k, v) in kwargs.items():
            params['es_{0}'.format(k)] = v

        params['es_q'] = query

        q = {
            'sort': [
                {"epoch": {"order": "desc"}},
            ]
        }

        q['query'] = {'term': {'query': query}},

        results = ES.search(q, index=self.name, **params)

        params['es_q'] = query
        for hit in results['hits']['hits']:
            yield Record._from_uuid(hit['_id'], collection=self.name)

    def search(self, query, sort=None, size=None, **kwargs):
        """Returns a list of Records for the given query."""

        if sort is not None:
            kwargs['sort'] = sort
        if size is not None:
            kwargs['size'] = size

        return [r for r in self.iter_search(query, **kwargs)]

    def save(self):
        # url_path = '{}/{}'.format(ELASTICSEARCH_URL, self.name)
        # return requests.post(url_path), auth=ES_AUTH)
        try:
            return ES.create_index(self.name)
        except IndexAlreadyExistsError:
            pass

    def new_record(self):
        r = Record()
        r.collection_name = self.name
        return r


class Record(object):
    """A record in the database."""

    def __init__(self):
        self.uuid = str(uuid4())
        self.data = {}
        self.epoch = epoch()
        self.collection_name = None

    def __repr__(self):
        return "<Record:{0}:{1} {2}>".format(self.collection_name,
                                             self.uuid, repr(self.data))

    def __getitem__(self, *args, **kwargs):
        return self.data.__getitem__(*args, **kwargs)

    def __setitem__(self, *args, **kwargs):
        return self.data.__setitem__(*args, **kwargs)

    def save(self):
        self.epoch = epoch()

        self._persist()
        self._index()

    def delete(self):
        ES.delete(index=self.collection.name, doc_type='record', id=self.uuid)
        BUCKET.delete_key('{}/{}'.format(self.collection.name, self.uuid))

    def _persist(self):
        """Saves the Record to S3."""
        key = BUCKET.new_key('{0}/{1}'.format(self.collection_name, self.uuid))
        key.update_metadata({'Content-Type': 'application/json'})
        key.set_contents_from_string(self.json)

    def _index(self):
        """Saves the Record to Elastic Search."""
        return ES.index(self.collection.name, 'record',
                        self.dict, id=self.uuid)

    @property
    def dict(self):
        d = self.data.copy()
        d.update(uuid=self.uuid, epoch=self.epoch)
        return d

    @property
    def json(self):
        return json.dumps({'record': self.dict})

    @property
    def collection(self):
        return Collection(name=self.collection_name)

    @classmethod
    def _from_uuid(cls, uuid, collection=None):
        if collection:
            uuid = '{}/{}'.format(collection, uuid)
        else:
            collection = uuid.split('/')[0]

        key = BUCKET.get_key(uuid)
        j = json.loads(key.read())['record']

        r = cls()
        r.collection_name = collection
        r.uuid = j.pop('uuid', None)
        r.epoch = j.pop('epoch', None)
        r.data = j

        return r


@manager.command
def seed():
    """Seeds the index from the configured S3 Bucket."""

    print 'Calculating Indexes...'
    indexes = set()
    for k in progress.bar([k for k in BUCKET.list()]):
        indexes.add(k.name.split('/')[0])

    print 'Creating Indexes...'
    for index in indexes:
        c = Collection(index)
        c.save()

    print 'Indexing...'
    for key in progress.bar([k for k in BUCKET.list()]):
        r = Record._from_uuid(key.name)
        r._index()


@manager.command
def purge():
    """Seeds the index from the configured S3 Bucket."""
    print 'Deleting all indexes...'
    ES.delete_all_indexes()


@app.before_request
def require_apikey():
    """Blocks aunauthorized requests."""

    if app.debug:
        return

    valid_key_param = request.args.get('key') == API_KEY
    valid_key_header = request.headers.get('X-Key') == API_KEY

    if request.authorization:
        valid_basic_pass = request.authorization.password == API_KEY
    else:
        valid_basic_pass = False

    if not (valid_key_param or valid_key_header or valid_basic_pass):
        return '>_<', 403


@app.route('/login')
def login_challenge():
    return Response('Could not verify your access level for that URL.\n'
                    'You have to login with proper credentials', 401,
                    {'WWW-Authenticate': 'Basic realm="Login Required"'})


@app.route('/<collection>/')
def get_collection(collection):
    """Get a list of records from a given collection."""

    if collection == 'favicon.ico':
        return '.'

    c = Collection(collection)

    args = request.args.to_dict()
    results = c.search(request.args.get('q'), **args)

    return jsonify(records=[r.dict for r in results])


@app.route('/<collection>/', methods=['POST', 'PUT'])
def post_collection(collection):
    """Add a new record to a given collection."""
    c = Collection(collection)
    c.save()

    record = c.new_record()
    record.data = request.json or request.form.to_dict()
    record.save()

    return get_record(collection, record.uuid)


@app.route('/<collection>/<uuid>')
def get_record(collection, uuid):
    """Get a record from a given colection."""
    return jsonify(record=Collection(collection)[uuid].dict)


@app.route('/<collection>/<uuid>', methods=['POST'])
def post_record(collection, uuid):
    """Replaces a given Record."""
    record = Collection(collection)[uuid]
    record.data = request.json or request.form.to_dict()
    record.save()

    return get_record(collection, uuid)


@app.route('/<collection>/<uuid>', methods=['PUT'])
def put_record(collection, uuid):
    """Updates a given Record."""

    record = Collection(collection)[uuid]
    record.data.update(request.json or request.form.to_dict())
    record.save()

    return get_record(collection, uuid)


@app.route('/<collection>/<uuid>', methods=['DELETE'])
def delete_record(collection, uuid):
    """Deletes a given record."""
    Collection(collection)[uuid].delete()
    return redirect('/{}/'.format(collection))

if __name__ == '__main__':
    manager.run()
