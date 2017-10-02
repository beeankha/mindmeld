import numpy as np
import tensorflow as tf
import re
import math
import logging
import os

from sklearn.externals import joblib
from .taggers import Tagger, extract_sequence_features
from .embeddings import LabelSequenceEmbedding, \
    WordSequenceEmbedding, \
    GazetteerSequenceEmbedding

DEFAULT_ENTITY_TOKEN_SPAN_INDEX = 2
GAZ_PATTERN_MATCH = 'in-gaz\|type:(\w+)\|pos:(\w+)\|'
REGEX_TYPE_POSITIONAL_INDEX = 1
DEFAULT_LABEL = 'B|UNK'
DEFAULT_PADDED_TOKEN = '<UNK>'
DEFAULT_GAZ_LABEL = 'O'
RANDOM_SEED = 1
ZERO_INITIALIZER_VALUE = 0

logger = logging.getLogger(__name__)


class LstmModel(Tagger):
    """"This class encapsulates the bi-directional LSTM model and provides
    the correct interface for use by the tagger model"""

    def fit(self, X, y):
        examples_arr = np.asarray(X, dtype='float32')
        labels_arr = np.asarray(y, dtype='int32')
        self._fit(examples_arr, labels_arr)
        return self

    def predict(self, X):
        encoded_examples_arr = np.asarray(X, dtype='float32')
        tags_by_example_arr = self._predict(encoded_examples_arr)

        resized_predicted_tags = []
        for query, seq_len in zip(tags_by_example_arr, self.sequence_lengths):
            resized_predicted_tags.append(query[:seq_len])

        return resized_predicted_tags

    def set_params(self, **parameters):
        """Initialize params

        Args:
            The keys in the parameters dictionary are as follows:

            number_of_epochs (int): The number of epochs to run
            batch_size (int): The batch size for mini-batch training
            token_lstm_hidden_state_dimension (int): The hidden state
            dimension of the LSTM cell
            learning_rate (int): The learning rate of the optimizer
            optimizer (str): The optimizer used to train the network
            is the number of entities in the dataset
            display_epoch (int): The number of epochs after which the
            network displays common stats like accuracy
            padding_length (int): The length of each query, which is
            fixed, so some queries will be cut short in length
            representing the word embedding, the row index
            is the word's index
            token_embedding_dimension (int): The embedding dimension of the word
            token_pretrained_embedding_filepath (str): The pretrained embedding file-path
            dense_keep_prob (float): The dropout rate of the dense layers
            lstm_input_keep_prob (float): The dropout rate of the inputs to the LSTM cell
            lstm_output_keep_prob (float): The dropout rate of the outputs of the LSTM cell
            gaz_encoding_dimension (int): The gazetteer encoding dimension
        """
        self.number_of_epochs = parameters.get('number_of_epochs', 20)
        self.batch_size = parameters.get('batch_size', 20)
        self.token_lstm_hidden_state_dimension = \
            parameters.get('token_lstm_hidden_state_dimension', 300)
        self.learning_rate = parameters.get('learning_rate', 0.005)
        self.optimizer_tf = parameters.get('optimizer', 'adam')
        self.padding_length = parameters.get('padding_length', 20)
        self.display_epoch = parameters.get('display_epoch', 20)
        self.token_embedding_dimension = parameters.get('token_embedding_dimension', 300)
        self.token_pretrained_embedding_filepath = \
            parameters.get('token_pretrained_embedding_filepath')
        self.dense_keep_probability = parameters.get('dense_keep_prob', 0.5)
        self.lstm_input_keep_prob = parameters.get('lstm_input_keep_prob', 0.5)
        self.lstm_output_keep_prob = parameters.get('lstm_output_keep_prob', 0.5)
        self.gaz_encoding_dimension = parameters.get('gaz_encoding_dimension', 100)
        self.use_crf_layer = parameters.get('use_crf_layer', True)

    def get_params(self, deep=True):
        return self.__dict__

    def construct_tf_variables(self):
        """
        Constructs the variables and operations in the tensorflow session graph
        """
        self.dense_keep_prob_tf = tf.placeholder(tf.float32, name='dense_keep_prob_tf')
        self.lstm_input_keep_prob_tf = tf.placeholder(tf.float32, name='lstm_input_keep_prob_tf')
        self.lstm_output_keep_prob_tf = tf.placeholder(tf.float32, name='lstm_output_keep_prob_tf')

        self.query_input_tf = tf.placeholder(tf.float32,
                                             [None,
                                              self.padding_length,
                                              self.token_embedding_dimension],
                                             name='query_input_tf')

        self.gaz_input_tf = tf.placeholder(tf.float32,
                                           [None,
                                            self.padding_length,
                                            self.gaz_dimension],
                                           name='gaz_input_tf')

        self.label_tf = tf.placeholder(tf.int32,
                                       [None,
                                        int(self.padding_length),
                                        self.output_dimension],
                                       name='label_tf')

        self.batch_sequence_lengths_tf = tf.placeholder(tf.int32, shape=[None],
                                                        name='batch_sequence_lengths_tf')

        word_and_gaz_embedding_tf = self._construct_embedding_network()
        self.lstm_output_tf = self._construct_lstm_network(word_and_gaz_embedding_tf)

        self.optimizer_tf, self.cost_tf = self._define_optimizer_and_cost(
            self.lstm_output_tf, self.label_tf, self.batch_sequence_lengths_tf)

    def extract_features(self, examples, config, resources, y=None, fit=True):
        if y:
            # Train time
            self.resources = resources

            # The gaz dimension are the sum total of the gazetteer entities and
            # the 'other' gaz entity, which is the entity for all non-gazetteer tokens
            self.gaz_dimension = len(self.resources['gazetteers'].keys()) + 1

            self.gaz_encoder = GazetteerSequenceEmbedding(self.padding_length,
                                                          DEFAULT_GAZ_LABEL,
                                                          self.gaz_dimension)

            encoded_labels = []
            for sequence in y:
                encoded_labels.append(self.label_encoder.encode_sequence_of_tokens(sequence))

            embedded_labels = self.label_encoder.get_embeddings_from_encodings(encoded_labels)

            self.output_dimension = len(self.label_encoder.token_to_encoding_mapping.keys())
        else:
            # Predict time
            embedded_labels = None

        # Extract features and classes
        x_sequence_embeddings_arr, self.gaz_features_arr = self._get_features(examples)
        self.sequence_lengths = self._extract_seq_length(examples)

        # There are no groups in this model
        groups = None

        return x_sequence_embeddings_arr, embedded_labels, groups

    def setup_model(self, config):

        self.set_params(**config.params)

        # We have to reset the graph on every dataset since the input, gaz and output
        # dimensions for each domain,intent training data is different. So the graph
        # cannot be reused.
        tf.reset_default_graph()
        self.session = tf.Session()
        self.example_type = config.example_type
        self.features = config.features
        self.token_pretrained_embedding_filepath = \
            config.params.get('token_pretrained_embedding_filepath')
        self.padding_length = config.params.get('padding_length')
        self.label_encoder = LabelSequenceEmbedding(self.padding_length, DEFAULT_LABEL)
        self.query_encoder = WordSequenceEmbedding(
            self.padding_length, DEFAULT_PADDED_TOKEN, True, self.token_embedding_dimension,
            self.token_pretrained_embedding_filepath)

    def construct_feed_dictionary(self,
                                  batch_examples,
                                  batch_gaz,
                                  batch_seq_len,
                                  batch_labels=list()):
        """Constructs the feed dictionary that is used to feed data into the tensors

        Args:
            batch_examples (ndarray): A batch of examples
            batch_gaz (ndarray): A batch of gazetteer features
            batch_seq_len (ndarray): A batch of sequence length of each query
            batch_labels (ndarray): A batch of labels

        Returns:
            The feed dictionary
        """
        return_dict = {
            self.query_input_tf: batch_examples,
            self.batch_sequence_lengths_tf: batch_seq_len,
            self.gaz_input_tf: batch_gaz,
            self.dense_keep_prob_tf: self.dense_keep_probability,
            self.lstm_input_keep_prob_tf: self.lstm_input_keep_prob,
            self.lstm_output_keep_prob_tf: self.lstm_output_keep_prob
        }

        if len(batch_labels) > 0:
            return_dict[self.label_tf] = batch_labels

        return return_dict

    def _construct_embedding_network(self):
        """ Constructs a network based on the word embedding and gazetteer
        inputs and concatenates them together

        Returns:
            Combined embeddings of the word and gazetteer embeddings
        """
        initializer = tf.contrib.layers.xavier_initializer(seed=RANDOM_SEED)

        dense_gaz_embedding_tf = tf.contrib.layers.fully_connected(
            inputs=self.gaz_input_tf,
            num_outputs=self.gaz_encoding_dimension,
            weights_initializer=initializer)

        # Combined the two embeddings
        combined_embedding_tf = tf.concat([self.query_input_tf, dense_gaz_embedding_tf], axis=2)
        return combined_embedding_tf

    def _define_optimizer_and_cost(self, output_tensor, label_tensor, sequence_lengths):
        """ This function defines the optimizer and cost function of the LSTM model

        Args:
            output_tensor (Tensor): Output tensor of the LSTM network
            label_tensor (Tensor): Label tensor of the true labels of the data
            sequence_lengths (Tensor): The tensor of sequence lengths for the current batch

        Returns:
            The optimizer function to reduce loss and the loss values
        """
        if self.use_crf_layer:
            flattened_labels = tf.cast(tf.argmax(label_tensor, axis=2), tf.int32)
            log_likelihood, transition_params = tf.contrib.crf.crf_log_likelihood(output_tensor,
                                                                                  flattened_labels,
                                                                                  sequence_lengths)
            cost_tf = tf.reduce_mean(-log_likelihood, name='cost_tf')
        else:
            softmax_loss_tf = tf.nn.softmax_cross_entropy_with_logits(
                logits=tf.reshape(output_tensor, [-1, self.output_dimension]),
                labels=tf.reshape(label_tensor, [-1, self.output_dimension]),
                name='softmax_loss_tf')
            cost_tf = tf.reduce_mean(softmax_loss_tf, name='cost_tf')

        optimizer_tf = tf.train.AdamOptimizer(learning_rate=float(self.learning_rate))\
            .minimize(cost_tf)
        return optimizer_tf, cost_tf

    def _calculate_score(self, output_arr, label_arr, seq_lengths_arr):
        """ This function calculates the sequence score of all the queries,
        that is, the total number of queries where all the tags are predicted
        correctly.

        Args:
            output_arr (ndarray): Output array of the LSTM network
            label_arr (ndarray): Label array of the true labels of the data
            seq_lengths_arr (ndarray): A real sequence lengths of each example

        Returns:
            The number of queries where all the tags are correct
        """
        reshaped_output_arr = np.reshape(
            output_arr, [-1, int(self.padding_length), self.output_dimension])
        reshaped_output_arr = np.argmax(reshaped_output_arr, 2)
        reshaped_labels_arr = np.argmax(label_arr, 2)

        score = 0
        for idx, query in enumerate(reshaped_output_arr):
            seq_len = seq_lengths_arr[idx]
            predicted_tags = reshaped_output_arr[idx][:seq_len]
            actual_tags = reshaped_labels_arr[idx][:seq_len]
            if np.array_equal(predicted_tags, actual_tags):
                score += 1

        return score

    def _construct_lstm_state(self, initializer, hidden_dimension, batch_size, name):
        """Construct the LSTM initial state

        Args:
            initializer (tf.contrib.layers.xavier_initializer): initializer used
            hidden_dimension: num dimensions of the hidden state variable
            batch_size: the batch size of the data
            name: suffix of the variable going to be used

        Returns:
            (LSTMStateTuple): LSTM state information
        """

        initial_cell_state = tf.get_variable(
            "initial_cell_state_{}".format(name),
            shape=[1, hidden_dimension],
            dtype=tf.float32,
            initializer=initializer)

        initial_output_state = tf.get_variable(
            "initial_output_state_{}".format(name),
            shape=[1, hidden_dimension],
            dtype=tf.float32,
            initializer=initializer)

        c_states = tf.tile(initial_cell_state, tf.stack([batch_size, 1]))
        h_states = tf.tile(initial_output_state, tf.stack([batch_size, 1]))

        return tf.contrib.rnn.LSTMStateTuple(c_states, h_states)

    def _construct_regularized_lstm_cell(self, hidden_dimensions, initializer):
        """Construct a regularized lstm cell based on a dropout layer

        Args:
            hidden_dimensions: num dimensions of the hidden state variable
            initializer (tf.contrib.layers.xavier_initializer): initializer used

        Returns:
            (DropoutWrapper): regularized LSTM cell
        """

        lstm_cell = tf.contrib.rnn.CoupledInputForgetGateLSTMCell(
            hidden_dimensions, forget_bias=1.0, initializer=initializer, state_is_tuple=True)

        lstm_cell = tf.contrib.rnn.DropoutWrapper(
            lstm_cell, input_keep_prob=self.lstm_input_keep_prob_tf,
            output_keep_prob=self.lstm_output_keep_prob_tf)

        return lstm_cell

    def _construct_lstm_network(self, input_tensor):
        """ This function constructs the Bi-Directional LSTM network

        Args:
            input_tensor (Tensor): Input tensor to the LSTM network

        Returns:
            output_tensor (Tensor): The output layer of the LSTM network
        """
        n_hidden = int(self.token_lstm_hidden_state_dimension)

        # We cannot use the static batch size variable since for the last batch set
        # of data, the data size could be less than the batch size
        batch_size_dim = tf.shape(input_tensor)[0]

        # We use the xavier initializer for some of it's gradient control properties
        initializer = tf.contrib.layers.xavier_initializer(seed=RANDOM_SEED)

        # Forward LSTM construction
        lstm_cell_forward_tf = self._construct_regularized_lstm_cell(n_hidden, initializer)
        initial_state_forward_tf = self._construct_lstm_state(
            initializer, n_hidden, batch_size_dim, 'lstm_cell_forward_tf')

        # Backward LSTM construction
        lstm_cell_backward_tf = self._construct_regularized_lstm_cell(n_hidden, initializer)
        initial_state_backward_tf = self._construct_lstm_state(
            initializer, n_hidden, batch_size_dim, 'lstm_cell_backward_tf')

        # Combined the forward and backward LSTM networks
        (output_fw, output_bw), _ = tf.nn.bidirectional_dynamic_rnn(
            cell_fw=lstm_cell_forward_tf,
            cell_bw=lstm_cell_backward_tf,
            inputs=input_tensor,
            sequence_length=self.batch_sequence_lengths_tf,
            dtype=tf.float32,
            initial_state_fw=initial_state_forward_tf,
            initial_state_bw=initial_state_backward_tf)

        # Construct the output later
        output_tf = tf.concat([output_fw, output_bw], axis=-1)
        output_tf = tf.nn.dropout(output_tf, self.dense_keep_prob_tf)

        output_weights_tf = tf.get_variable('output_weights_tf',
                                            shape=[2 * n_hidden, self.output_dimension],
                                            dtype="float32", initializer=initializer)
        output_weights_tf = tf.tile(output_weights_tf, [batch_size_dim, 1])
        output_weights_tf = tf.reshape(output_weights_tf, [batch_size_dim, 2 * n_hidden,
                                       self.output_dimension])

        zero_initializer = tf.constant_initializer(ZERO_INITIALIZER_VALUE)
        output_bias_tf = tf.get_variable('output_bias_tf', shape=[self.output_dimension],
                                         dtype="float32", initializer=zero_initializer)

        output_tf = tf.add(tf.matmul(output_tf, output_weights_tf), output_bias_tf,
                           name='output_tensor')
        return output_tf

    def _get_model_constructor(self):
        return self

    def _extract_seq_length(self, examples):
        """Extract sequence lengths from the input examples
        Args:
            examples (list of Query objects): List of input queries

        Returns:
            (list): List of seq lengths for each query
        """
        seq_lengths = []
        for example in examples:
            if len(example.normalized_tokens) > self.padding_length:
                seq_lengths.append(self.padding_length)
            else:
                seq_lengths.append(len(example.normalized_tokens))

        return seq_lengths

    def _get_features(self, examples):
        """Extracts the word and gazetteer embeddings from the input examples

        Args:
            examples (list of mmworkbench.core.Query): a list of queries
        Returns:
            (tuple): Word embeddings and Gazetteer one-hot embeddings
        """
        x_feats_array = []
        gaz_feats_array = []
        for idx, example in enumerate(examples):
            x_feat, gaz_feat = self._extract_features(example)
            x_feats_array.append(x_feat)
            gaz_feats_array.append(gaz_feat)

        x_feats_array = self.query_encoder.get_embeddings_from_encodings(x_feats_array)
        gaz_feats_array = self.gaz_encoder.get_embeddings_from_encodings(gaz_feats_array)
        return x_feats_array, gaz_feats_array

    def _extract_features(self, example):
        """Extracts feature dicts for each token in an example.

        Args:
            example (mmworkbench.core.Query): an query
        Returns:
            (list dict): features
        """
        extracted_gaz_tokens = []
        extracted_sequence_features = extract_sequence_features(
            example, self.example_type, self.features, self.resources)

        for index, extracted_gaz in enumerate(extracted_sequence_features):
            if extracted_gaz == {}:
                extracted_gaz_tokens.append(DEFAULT_GAZ_LABEL)
                continue

            combined_gaz_features = set()
            for key in extracted_gaz.keys():
                regex_match = re.match(GAZ_PATTERN_MATCH, key)
                if regex_match:
                    # Examples of gaz features here are:
                    # in-gaz|type:city|pos:start|p_fe,
                    # in-gaz|type:city|pos:end|pct-char-len
                    # There were many gaz features of the same type that had
                    # bot start and end position tags for a given token.
                    # Due to this, we did not implement functionality to
                    # extract the positional information due to the noise
                    # associated with it.
                    combined_gaz_features.add(
                        regex_match.group(REGEX_TYPE_POSITIONAL_INDEX))

            if len(combined_gaz_features) == 0:
                extracted_gaz_tokens.append(DEFAULT_GAZ_LABEL)
            else:
                extracted_gaz_tokens.append(",".join(list(combined_gaz_features)))

        assert len(extracted_gaz_tokens) == len(example.normalized_tokens), \
            "The length of the gaz and example query have to be the same"

        encoded_gaz = self.gaz_encoder.encode_sequence_of_tokens(extracted_gaz_tokens)
        padded_query = self.query_encoder.encode_sequence_of_tokens(example.normalized_tokens)

        return padded_query, encoded_gaz

    def _fit(self, X, y):
        """Trains a classifier without cross-validation. It iterates through
        the data, feeds batches to the tensorflow session graph and fits the
        model based on the feed forward and back propagation steps.

        Args:
            X (list of list of list of str): a list of queries to train on
            y (list of list of str): a list of expected labels
        """
        self.construct_tf_variables()

        self.session.run([tf.global_variables_initializer(),
                          tf.local_variables_initializer()])

        for epochs in range(int(self.number_of_epochs)):
            logger.info("Training epoch : {}".format(epochs))

            indices = [x for x in range(len(X))]
            np.random.shuffle(indices)

            gaz = self.gaz_features_arr[indices]
            examples = X[indices]
            labels = y[indices]
            batch_size = int(self.batch_size)
            num_batches = int(math.ceil(len(examples) / batch_size))
            seq_len = np.array(self.sequence_lengths)[indices]

            for batch in range(num_batches):
                batch_start_index = batch * batch_size
                batch_end_index = (batch * batch_size) + batch_size

                batch_examples = examples[batch_start_index:batch_end_index]
                batch_labels = labels[batch_start_index:batch_end_index]
                batch_gaz = gaz[batch_start_index:batch_end_index]
                batch_seq_len = seq_len[batch_start_index:batch_end_index]

                if batch % int(self.display_epoch) == 0:
                    output, loss, _ = self.session.run([self.lstm_output_tf,
                                                        self.cost_tf,
                                                        self.optimizer_tf],
                                                       feed_dict=self.construct_feed_dictionary(
                                                        batch_examples,
                                                        batch_gaz,
                                                        batch_seq_len,
                                                        batch_labels))

                    score = self._calculate_score(output, batch_labels, batch_seq_len)
                    accuracy = score / (len(batch_examples) * 1.0)

                    logger.info("Trained batch from index {} to {}, "
                                "Mini-batch loss: {:.5f}, "
                                "Training sequence accuracy: {:.5f}".format(batch * batch_size,
                                                                            (batch * batch_size) +
                                                                            batch_size, loss,
                                                                            accuracy))
                else:
                    self.session.run(self.optimizer_tf, feed_dict=self.construct_feed_dictionary(
                        batch_examples, batch_gaz, batch_seq_len, batch_labels))
        return self

    def _predict(self, X):
        """Trains a classifier without cross-validation.

        Args:
            X (list of list of list of str): a list of queries to train on

        Returns:
            (list): A list of decoded labelled predicted by the model
        """
        seq_len_arr = np.array(self.sequence_lengths)

        # During predict time, we make sure no nodes are dropped out
        self.dense_keep_probability = 1.0
        self.lstm_input_keep_prob = 1.0
        self.lstm_output_keep_prob = 1.0

        output = self.session.run(
            [self.lstm_output_tf],
            feed_dict=self.construct_feed_dictionary(X, self.gaz_features_arr, seq_len_arr))

        output = np.reshape(output, [-1, int(self.padding_length), self.output_dimension])
        output = np.argmax(output, 2)

        decoded_queries = []
        for idx, encoded_predict in enumerate(output):
            decoded_query = []
            for tag in encoded_predict[:self.sequence_lengths[idx]]:
                decoded_query.append(self.label_encoder.encoding_to_token_mapping[tag])
            decoded_queries.append(decoded_query)

        return decoded_queries

    def dump(self, path='lstm_model'):
        """
        Saves the Tensorflow model
        """
        # Save the tensorflow weights and variables
        saver = tf.train.Saver()
        saver.save(self.session, os.path.join(path, 'lstm_model'))

        # Save feature extraction variables
        variables_to_dump = {
            'resources': self.resources,
            'gaz_dimension': self.gaz_dimension,
            'output_dimension': self.output_dimension,
            'gaz_features': self.gaz_features_arr,
            'sequence_lengths': self.sequence_lengths,
            'gaz_encoder': self.gaz_encoder,
            'label_encoder': self.label_encoder
        }

        joblib.dump(variables_to_dump, os.path.join(path, '.feature_extraction_vars'))

    def load(self, path='lstm-model'):
        """
        Loads the Tensorflow model
        """
        saver = tf.train.import_meta_graph(os.path.join(path, 'lstm_model.meta'))
        saver.restore(self.session, os.path.join(path, 'lstm_model'))

        # Restore tensorflow graph variables

        self.dense_keep_prob_tf = \
            self.session.graph.get_tensor_by_name('dense_keep_prob_tf:0')

        self.lstm_input_keep_prob_tf = \
            self.session.graph.get_tensor_by_name('lstm_input_keep_prob_tf:0')

        self.lstm_output_keep_prob_tf = \
            self.session.graph.get_tensor_by_name('lstm_output_keep_prob_tf:0')

        self.query_input_tf = self.session.graph.get_tensor_by_name('query_input_tf:0')

        self.gaz_input_tf = self.session.graph.get_tensor_by_name('gaz_input_tf:0')

        self.label_tf = self.session.graph.get_tensor_by_name('label_tf:0')

        self.batch_sequence_lengths_tf = \
            self.session.graph.get_tensor_by_name('sequence_length_tf:0')

        self.lstm_output_tf = self.session.graph.get_tensor_by_name('output_tensor:0')

        # Load feature extraction variables
        variables_to_load = joblib.load(os.path.join(path, '.feature_extraction_vars'))
        self.resources = variables_to_load['resources']
        self.gaz_dimension = variables_to_load['gaz_dimension']
        self.output_dimension = variables_to_load['output_dimension']
        self.gaz_features = variables_to_load['gaz_features']
        self.sequence_lengths = variables_to_load['sequence_lengths']
        self.gaz_encoder = variables_to_load['gaz_encoder']
        self.label_encoder = variables_to_load['label_encoder']