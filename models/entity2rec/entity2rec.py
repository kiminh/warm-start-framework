from __future__ import print_function
import codecs
import json
import numpy as np
import pandas as pd
import joblib
from loguru import logger

from models.entity2rec.entity2vec import Entity2Vec
from models.entity2rec.entity2rel import Entity2Rel
import pyltr
import sys

from models.entity2rec.metrics import precision_at_n

sys.path.append('.')
from collections import defaultdict
import heapq


class Property:
    def __init__(self, name, typology):
        self.name = name
        self._typology = typology

    @property
    def typology(self):

        return self._typology

    @typology.setter
    def typology(self, value):

        if value != 'collaborative' and value != 'content' and value != 'social':
            raise ValueError('Type of property can be: collaborative, content or social')
        else:
            self._typology = value


class Entity2Rec(Entity2Vec, Entity2Rel):
    name = 'Entity2Rec'

    """Computes a set of relatedness scores between user-item pairs from a set of property-specific Knowledge Graph
    embeddings and user feedback and feeds them into a learning to rank algorithm"""

    def __init__(self, split, training, run_all=True,
                 is_directed=False, preprocessing=True, is_weighted=False,
                 p=1, q=1, walk_length=100,
                 num_walks=50, dimensions=200, window_size=30,
                 workers=8, iterations=5, config='config/properties.json',
                 feedback_file=False, collab_only=False, content_only=False,
                 social_only=False):

        Entity2Vec.__init__(self, is_directed, preprocessing, is_weighted, p, q, walk_length, num_walks, dimensions,
                            window_size, workers, iterations, feedback_file, split, training)

        Entity2Rel.__init__(self, split)

        self.config_file = config
        self.dataset = split.experiment.dataset.name
        self.properties = []
        self._set_properties()

        # run entity2vec to create the embeddings
        if run_all:
            logger.debug('Running entity2vec to generate property-specific embeddings...')
            properties_names = []

            for prop in self.properties:
                properties_names.append(prop.name)

            self.e2v_walks_learn(properties_names, self.dataset)  # run entity2vec

        # reads the embedding files
        self._set_embedding_files()

        # initialize model to None
        self.model = None

        # whether using only collab or content features
        self.collab_only = collab_only
        self.content_only = content_only
        self.social_only = social_only

        # initialize cluster models
        self.models = {}
        self.user_to_cluster = None

    def _set_properties(self):
        triples = pd.read_csv(self.split.experiment.dataset.triples_path)
        relations = set(triples['relation'])
        for relation in relations:
            self.properties.append(Property(relation, 'content'))

        self.properties.append(Property('feedback', 'collaborative'))

    def _set_embedding_files(self):

        """
        Creates the dictionary of embedding files
        """

        for prop in self.properties:
            prop_name = prop.name
            prop_short = prop_name
            if '/' in prop_name:
                prop_short = prop_name.split('/')[-1]

            if prop_name == 'feedback':
                splitting = self.split.experiment.name
                number = self.split.name.split('.')[0]
                emb_file = "emb/%s/%s/num%d_p%d_q%d_l%d_d%d_iter" \
                             "%d_winsize%d%s-%s.emd" % (self.dataset, prop_short, self.num_walks, int(self.p),
                                                        int(self.q), self.walk_length, self.dimensions, self.iter,
                                                        self.window_size, splitting, number)
            else:
                emb_file = u'emb/%s/%s/num%s_p%d_q%d_l%s_d%s_iter%d_winsize%d.emd' % (
                    self.dataset, prop_short, self.num_walks, int(self.p), int(self.q), self.walk_length, self.dimensions,
                    self.iter,
                    self.window_size)

            self.add_embedding(prop_name, emb_file)

    def collab_similarities(self, user, item):

        # collaborative properties

        collaborative_properties = [prop for prop in self.properties if prop.typology == "collaborative"]

        sims = []

        for prop in collaborative_properties:
            sims.append(self.relatedness_score(prop.name, user, item))

        return sims

    def content_similarities(self, user, item, items_liked_by_user):

        # content properties

        content_properties = [prop for prop in self.properties if prop.typology == "content"]

        sims = []

        if not items_liked_by_user:  # no past positive feedback

            sims = [0. for i in range(len(content_properties))]

        else:

            for prop in content_properties:  # append a list of property-specific scores

                sims_prop = []

                for past_item in items_liked_by_user:
                    sims_prop.append(self.relatedness_score(prop.name, past_item, item))

                s = np.mean(sims_prop)

                sims.append(s)

        return sims

    def social_similarities(self, user, item, users_liking_the_item):

        # social properties

        social_properties = [prop for prop in self.properties if prop.typology == "social"]

        sims = []

        if not users_liking_the_item:

            sims = [0. for i in range(len(social_properties))]

        else:

            for prop in social_properties:  # append a list of property-specific scores

                sims_prop = []

                for past_user in users_liking_the_item:
                    sims_prop.append(self.relatedness_score(prop.name, past_user, user))

                s = np.mean(sims_prop)

                sims.append(s)

        return sims

    def _compute_scores(self, user, item, items_liked_by_user, users_liking_the_item):

        collab_score = self.collab_similarities(user, item)

        content_scores = self.content_similarities(user, item, items_liked_by_user)

        social_scores = self.social_similarities(user, item, users_liking_the_item)

        return collab_score, content_scores, social_scores

    def compute_user_item_features(self, user, item, items_liked_by_user, users_liking_the_item):

        collab_scores, content_scores, social_scores = self._compute_scores(user, item, items_liked_by_user,
                                                                            users_liking_the_item)

        if self.collab_only:

            features = collab_scores

        elif self.content_only:

            features = content_scores

        elif self.social_only:

            features = social_scores

        else:

            features = collab_scores + content_scores + social_scores

        return features

    def fit(self, x_train, y_train, qids_train, x_val=None, y_val=None, qids_val=None,
            optimize='P', N=5, lr=0.1, n_estimators=100, max_depth=5,
            max_features=None, user_to_cluster=None):

        # choose the metric to optimize during the fit process

        if not N:
            N = 10 ** 8

        if optimize == 'NDCG':
            fit_metric = pyltr.metrics.NDCG(k=N)
        elif optimize == 'P':
            fit_metric = precision_at_n.PrecisionAtN(k=N)
        elif optimize == 'AP':
            fit_metric = pyltr.metrics.AP(k=N)
        else:
            raise ValueError('Metric not implemented')

        self.model = pyltr.models.LambdaMART(
            metric=fit_metric,
            n_estimators=n_estimators,
            learning_rate=lr,
            max_depth=max_depth,
            max_features=max_features,
            verbose=1,
            random_state=1
        )

        # Only needed if you want to perform validation (early stopping & trimming)

        if x_val is not None and y_val is not None and qids_val is not None:

            monitor = pyltr.models.monitors.ValidationMonitor(
                x_val, y_val, qids_val, metric=fit_metric)

            self.model.fit(x_train, y_train, qids_train, monitor=monitor)
        else:
            self.model.fit(x_train, y_train, qids_train)

    def predict(self, x_test, qids_test):

        if self.user_to_cluster:

            preds = []

            for i, line in enumerate(x_test):
                qid = str(qids_test[i])

                cluster = self.user_to_cluster[qid]

                # retrieve the corresponding model of that cluster
                model = self.models[cluster]

                preds.append(model.predict(line.reshape(1, -1)))

            return preds

        else:

            if self.model:

                return self.model.predict(x_test)

            else:

                return list(map(lambda x: np.mean(x), x_test))

    def save_model(self, model_file):

        if not self.model:

            joblib.dump(self.model, model_file)

        else:

            raise AttributeError('Fit the model before saving it')

    def load_model(self, model_file):

        self.model = joblib.load(model_file)

    def recommend(self, user, qids_test, x_test, items_test, N=10, average=True):

        indeces = np.where(qids_test == user)  # find indeces corresponding to user

        features = x_test[indeces]  # find features corresponding to user

        candidates = items_test[indeces]  # find candidate items corresponding to users

        if not average:

            preds = self.model.predict(features)

        else:

            preds = list(map(lambda x: np.mean(x), features))

        candidates_index = {i: [candidate, (self.properties[np.argmax(features[i])]).name] for i, candidate in
                            enumerate(candidates)}

        recs_index = heapq.nlargest(N, candidates_index.keys(), key=lambda x: preds[x])

        recs = [candidates_index[index] for index in recs_index]

        return recs