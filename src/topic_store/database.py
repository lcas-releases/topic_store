#  Raymond Kirk (Tunstill) Copyright (c) 2020
#  Email: ray.tunstill@gmail.com

# This file contains the interface to a mongo_db client

from __future__ import absolute_import, division, print_function

import rospkg
import bson
import gridfs
import pymongo
from copy import copy

import pathlib

from topic_store import get_package_root
from topic_store.api import Storage
from topic_store.data import TopicStore, MongoDBReverseParser, MongoDBParser
from topic_store.file_parsers import load_yaml_file
from topic_store.scenario import ScenarioFileParser

try:
    from collections import Mapping as MappingType
except ImportError:
    from collections.abc import Mapping as MappingType

__all__ = ["MongoStorage"]


class MongoStorage(Storage):
    """Uses PyMongo and YAML config connection interface see ($(find topic_store)/config/default_db_config.yaml)
        Config options available at (https://docs.mongodb.com/manual/reference/configuration-options/). Will use the
        net.bindIp and net.port parameters of net in config.yaml to interface with a MongoDB server. Interface is the
        same as TopicStorage and pymongo utilities wrapped in this class to ensure TopicStore objects returned where
        possible.
    """
    suffix = ".yaml"

    def __init__(self, config=None, collection="default", uri=None):
        """

        Args:
            config: Path to MongoDB config file that a URI can be inferred from
            collection: The collection to manage
            uri: URI overload, if passed will attempt to connect directly and config not used
        """
        self.uri = uri
        if self.uri is None:
            if config in ["topic_store", "auto", "default"] or config is None:
                config = get_package_root() / "config" / "default_db_config.yaml"
            self.uri = self.uri_from_mongo_config(config)

        self.parser = MongoDBParser()  # Adds support for unicode to python str etc
        self.reverse_parser = MongoDBReverseParser()  # Adds support for unicode to python str etc
        self.name = "topic_store"
        self.collection_name = collection

        self.client = pymongo.MongoClient(self.uri)
        self._db = self.client[self.name]
        self._fs = gridfs.GridFS(self._db, collection=self.collection_name)
        self.collection = self._db[self.collection_name]

    @staticmethod
    def uri_from_mongo_config(mongo_config_path):
        # TODO: Add support for user/password in the config file and TLS/Auth options to MongoClient
        if isinstance(mongo_config_path, str):
            mongo_config_path = pathlib.Path(mongo_config_path)
        if not mongo_config_path.is_file() or mongo_config_path.suffix != ".yaml":
            raise IOError("'{}' is not a valid MongoDB configuration file".format(mongo_config_path))
        mongo_config = load_yaml_file(mongo_config_path)
        uri = "mongodb://{}:{}".format(mongo_config["net"]["bindIp"], mongo_config["net"]["port"])
        return uri

    @staticmethod
    def load(path):
        """Loads connection information from a .yaml scenario file"""
        path = MongoStorage.parse_path(path, require_suffix=MongoStorage.suffix)
        scenario = ScenarioFileParser(path).require_database()
        return MongoStorage(config=scenario.storage["config"], collection=scenario.context)

    def __apply_fn_to_nested_dict(self, original_dict, iter_dict=None, fn=None):
        if iter_dict is None:
            iter_dict = copy(original_dict)
        for k, v in iter_dict.iteritems():
            if isinstance(v, MappingType):
                original_dict[k] = self.__apply_fn_to_nested_dict(original_dict.get(k, {}), v, fn)
            else:
                fk, fv = k, v
                if fn is not None:
                    fk, fv = fn(k, v)
                original_dict[k] = fv
                original_dict[fk] = original_dict.pop(k)
        return original_dict

    def __gridfs_ify(self, topic_store):
        """Places all bson.binary.Binary types in the gridfs files/storage system so no limit on 16MB documents"""
        def __grid_fs_binary_objects(k, v):
            if isinstance(v, bson.binary.Binary):
                return "__gridfs_file_" + k, self._fs.put(v)
            return k, v
        parsed_dict = self.parser(topic_store.dict.copy())
        return self.__apply_fn_to_nested_dict(parsed_dict, fn=__grid_fs_binary_objects)

    def __ungridfs_ify(self, python_dict):
        """Gets all bson.binary.Binary types from the gridfs files/storage system"""
        def __populate_grid_fs_files(k, v):
            if k.startswith("__gridfs_file_") and isinstance(v, bson.objectid.ObjectId):
                return k.replace("__gridfs_file_", ""), bson.binary.Binary(self._fs.get(v).read())
            return k, v
        return self.__apply_fn_to_nested_dict(python_dict, fn=__populate_grid_fs_files)

    def insert_one(self, topic_store):
        """Inserts a topic store object into the database

        Returns:
            pymongo.results.InsertOneResult: Contains the ID for the inserted document
        """
        if not isinstance(topic_store, TopicStore):
            raise ValueError("Can only insert TopicStore items into the database not '{}'".format(type(topic_store)))

        parsed_store = self.__gridfs_ify(topic_store)
        return self.collection.insert_one(parsed_store)

    def update_one(self, query, update, *args, **kwargs):
        """Updates a single document matched by query"""
        return self.collection.update_one(query, update, *args, **kwargs)

    def update_one_by_id(self, id_str, **kwargs):
        """Update a document field by ID changes all keys in kwargs"""
        return self.update_one(query={'_id': id_str}, update={"$set": kwargs})

    def find(self, *args, **kwargs):
        """Returns TopicStoreCursor to all documents in the query"""
        return TopicStoreCursor(self.collection.find(*args, **kwargs), apply_fn=self.__ungridfs_ify)

    __iter__ = find

    def find_one(self, query, *args, **kwargs):
        """Returns a matched TopicStore document"""
        parsed_document = self.reverse_parser(self.collection.find_one(query, *args, **kwargs))
        parsed_document = self.__ungridfs_ify(parsed_document)
        return TopicStore(parsed_document)

    def find_by_id(self, id_str, *args, **kwargs):
        """Returns a matched TopicStore document"""
        return self.find_one({"_id": id_str}, *args, **kwargs)

    def find_by_session_id(self, session_id, *args, **kwargs):
        """Returns matched TopicStore documents collected in the same session"""
        return self.find({"_ts_meta.session": session_id}, *args, **kwargs)

    def get_unique_sessions(self):
        """Returns IDs of unique data collections scenario runs in the collection"""
        return dict((x["_id"], {"time": x["sys_time"], "count": x["count"]}) for x in self.collection.aggregate([{
            '$match': {'_ts_meta.session': {'$exists': True}}},
            {'$group': {'_id': '$_ts_meta.session', 'sys_time': {'$first': "$_ts_meta.sys_time"}, "count": {'$sum': 1}}}
        ]))

    def delete_by_id(self, id_str, *args, **kwargs):
        """Deletes a document by id"""
        def __delete_gridfs_docs(k, v):
            if k.startswith("__gridfs_file_") and isinstance(v, bson.objectid.ObjectId):
                self._fs.delete(v)
            return k, v
        parsed_document = self.reverse_parser(self.collection.find_one({"_id": id_str}, *args, **kwargs))
        self.__apply_fn_to_nested_dict(parsed_document, fn=__delete_gridfs_docs)
        return self.collection.delete_one({"_id": id_str}, *args, **kwargs)

    def __aggregate(self, pipeline, *args, **kwargs):
        """Returns TopicStoreCursor of the aggregate pipeline match in a collection"""
        raise NotImplementedError("Not yet implemented since aggregate pipelines can be non TopicStore compatible docs")
        # return TopicStoreCursor(self.collection.aggregate(pipeline, *args, **kwargs))


class TopicStoreCursor:
    """Wrapper for a pymongo.cursor.Cursor object to return documents as the TopicStore"""
    def __init__(self, cursor, apply_fn=None):
        self.parser = MongoDBReverseParser()
        self.apply_fn = apply_fn
        # Copy the cursor to this parent class
        self.cursor = cursor

    def __getitem__(self, item):
        document = self.parser(self.cursor.__getitem__(item))
        if self.apply_fn:
            document = self.apply_fn(document)
        return TopicStore(document)

    def next(self):
        document = self.parser(self.cursor.next())
        if self.apply_fn:
            document = self.apply_fn(document)
        return TopicStore(document)

    __next__ = next


class MongoServer:
    def __init__(self, debug=False):
        if not debug:
            raise NotImplementedError("Server is not yet implemented. Please call start_database.launch.")
        import subprocess
        import pathlib
        import rospy
        import os
        pkg_root = get_package_root()
        script_path = pkg_root / "docker/docker_compose_up_safe.sh"
        db_default = pathlib.Path(os.path.expanduser("~/.ros/topic_store/database"))
        rospy.on_shutdown(self._on_shutdown)
        self.process = subprocess.Popen(['bash', script_path], env={"MONGO_DB_PATH": db_default})

    def _on_shutdown(self):
        self.process.wait()

    __del__ = _on_shutdown
