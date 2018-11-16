from __future__ import absolute_import
from __future__ import division
from __future__ import print_function 

import os
import numpy as np
import tensorflow as tf
import random
import argparse
import math
import sys
from tqdm import *

from handler import Batch, DatasetBase, DatasetTrain, DatasetEval, DatasetTest
import util
from gensim.models import Word2Vec


FLAGS = None

np.random.seed(0)
tf.set_random_seed(0)

filename = '/clr_conversation.txt'
total_line_num = 2842478
train_line_num = 2840000
eval_line_num  =    2478

emb_size       =     300
PKL_EXIST      =    False

MAX_SENTENCE_LENGTH = 15 # longest
special_tokens = {'<PAD>': 0, '<BOS>': 1, '<EOS>': 2, '<UNK>': 3}
special_tokens_to_word = ['<PAD>', '<BOS>', '<EOS>', '<UNK>']

modes = {'train': 0, 'eval': 1, 'test': 2}

class Seq2Seq:
    def __init__(self, voc, idx2word, mode, att, lr=None):


        self.num_layers     =     2
        self.rnn_size       =   2048
        self.keep_prob      =   1.0
        self.vocab_num      =   voc
        self.with_attention =   att
        self.mode           =  mode
        self.lr             =    lr
        self.idx2word   =  idx2word
        self.embedding_size =   emb_size
        self.hist_summary   =    []
        

    def _create_rnn_cell(self):

        def single_rnn_cell():
            cell = tf.contrib.rnn.GRUCell(self.rnn_size)
            #cell = tf.contrib.rnn.LSTMCell(self.rnn_size, initializer=tf.orthogonal_initializer())
            if self.mode == modes['train']:
                cell = tf.contrib.rnn.DropoutWrapper(cell, input_keep_prob=self.keep_prob, 
                        output_keep_prob=self.keep_prob)
            return cell
        cell = tf.contrib.rnn.MultiRNNCell([single_rnn_cell() for _ in range(self.num_layers)])
        return cell

    def build_model(self, emb=None):
        
        self.encoder_inputs = tf.placeholder(tf.int32, [None, None], name='encoder_inputs')
        self.encoder_inputs_length = tf.placeholder(tf.int32, [None], name='encoder_inputs_length')

        with tf.device("/cpu:0"):
            embed = tf.constant_initializer(emb, dtype=tf.float32)
            embedding = tf.get_variable(
                initializer=embed, shape=emb.shape, dtype=tf.float32, trainable=True, name='embedding')
        #embedding = tf.get_variable('embedding', [self.vocab_num, self.embedding_size])
        self.hist_summary.append(tf.summary.histogram(embedding.name + '/embed', embedding))

        self.batch_size = tf.placeholder(tf.int32, [], name='batch_size')
        self.sampling_prob = tf.placeholder(tf.float32, [], name='sampling_prob')

        if self.mode != modes['test']:
            self.decoder_targets = tf.placeholder(tf.int32, [None, None], name='decoder_targets')
            self.decoder_targets_length = tf.placeholder(tf.int32, [None], name='decoder_targets_length')
            self.max_target_sequence_length = tf.reduce_max(self.decoder_targets_length, name='max_target_len')
            self.mask = tf.sequence_mask(self.decoder_targets_length, self.max_target_sequence_length, 
                dtype=tf.float32, name='masks')

        with tf.variable_scope('encoder'):
            encoder_cell = self._create_rnn_cell()
            with tf.device("/cpu:0"):
                encoder_inputs_embedded = tf.nn.embedding_lookup(embedding, self.encoder_inputs)
            encoder_outputs, encoder_state = tf.nn.dynamic_rnn(encoder_cell, encoder_inputs_embedded,
                                                               sequence_length=self.encoder_inputs_length,
                                                               dtype=tf.float32)
        with tf.variable_scope('decoder'):
            encoder_inputs_length = self.encoder_inputs_length
            decoder_cell = self._create_rnn_cell()
            batch_size = self.batch_size
            if self.with_attention:
                print('wrapped with bahdanau attention...')
                attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
                    num_units=self.rnn_size, memory=encoder_outputs, 
                    memory_sequence_length=encoder_inputs_length)
                decoder_cell = tf.contrib.seq2seq.AttentionWrapper(
                    cell=decoder_cell, attention_mechanism=attention_mechanism,
                    attention_layer_size=self.rnn_size, name='Attention_Wrapper')
                decoder_initial_state = decoder_cell.zero_state(batch_size=batch_size, 
                    dtype=tf.float32).clone(cell_state=encoder_state)
            else:
                decoder_initial_state = encoder_state
            projection_layer = tf.layers.Dense(
                    self.vocab_num, kernel_initializer=tf.truncated_normal_initializer(mean=0.0, stddev=0.1))
                    #self.vocab_num, kernel_initializer=tf.contrib.layers.xavier_initializer())

            if self.mode == modes['train']:
                ending = tf.strided_slice(self.decoder_targets, [0, 0], [self.batch_size, -1], [1, 1])
                decoder_input = tf.concat([tf.fill([self.batch_size, 1], special_tokens['<BOS>']), ending], 1)
                with tf.device("/cpu:0"):
                    decoder_inputs_embedded = tf.nn.embedding_lookup(embedding, decoder_input)
                training_helper = tf.contrib.seq2seq.ScheduledEmbeddingTrainingHelper(
                                                                    inputs=decoder_inputs_embedded,
                                                                    sequence_length=self.decoder_targets_length,
                                                                    embedding=embedding,
                                                                    time_major=False, 
                                                                    sampling_probability=self.sampling_prob,
                                                                    name='training_helper')
                training_decoder = tf.contrib.seq2seq.BasicDecoder(cell=decoder_cell, helper=training_helper,
                      initial_state=decoder_initial_state, output_layer=projection_layer)
                decoder_outputs, _, final_seq_len = tf.contrib.seq2seq.dynamic_decode(decoder=training_decoder,
                                                                          impute_finished=True,
                                                                maximum_iterations=self.max_target_sequence_length)
                self.decoder_logits_train = tf.identity(decoder_outputs.rnn_output)
                self.decoder_predict_train = tf.argmax(self.decoder_logits_train, axis=-1, name='decoder_pred_train')
                print(self.decoder_predict_train)
                self.train_loss = tf.contrib.seq2seq.sequence_loss(logits=self.decoder_logits_train,
                                                             targets=self.decoder_targets, weights=self.mask)
                self.train_summary = tf.summary.scalar('training loss', self.train_loss)

            elif self.mode == modes['eval']:
                start_tokens = tf.ones([self.batch_size, ], tf.int32) # * special_tokens['<BOS>']
                end_token = special_tokens['<EOS>']
                decoding_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(embedding=embedding,
                                                                    start_tokens=start_tokens, end_token=end_token)
                inference_decoder = tf.contrib.seq2seq.BasicDecoder(cell=decoder_cell, helper=decoding_helper,
                                                                        initial_state=decoder_initial_state,
                                                                        output_layer=projection_layer)
                decoder_outputs, _, final_seq_len = tf.contrib.seq2seq.dynamic_decode(decoder=inference_decoder,
                                                                maximum_iterations=self.max_target_sequence_length)
                # pad to same shape in order to calculate loss
                pad_size = self.max_target_sequence_length - tf.reduce_max(final_seq_len)
                pad_rnn_output = tf.pad(decoder_outputs.rnn_output, [[0, 0], [0, pad_size], [0, 0]])
                
               #  self.decoder_logits_eval = tf.identity(decoder_outputs.rnn_output)
                self.decoder_logits_eval = tf.identity(pad_rnn_output)
                self.decoder_predict_eval = tf.argmax(self.decoder_logits_eval, axis=-1, name='decoder_pred_eval')
                print(self.decoder_predict_eval)
                self.eval_loss = tf.contrib.seq2seq.sequence_loss(logits=pad_rnn_output,
                                                             targets=self.decoder_targets, weights=self.mask)

                self.eval_summary = tf.summary.scalar('validation loss', self.eval_loss)

            elif self.mode == modes['test']:
                start_tokens = tf.ones([self.batch_size, ], tf.int32) # * special_tokens['<BOS>']
                end_token = special_tokens['<EOS>']
                decoding_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(embedding=embedding,
                                                                    start_tokens=start_tokens, end_token=end_token)
                inference_decoder = tf.contrib.seq2seq.BasicDecoder(cell=decoder_cell, helper=decoding_helper,
                                                                        initial_state=decoder_initial_state,
                                                                        output_layer=projection_layer)
                decoder_outputs, _, final_seq_len = tf.contrib.seq2seq.dynamic_decode(decoder=inference_decoder,
                                                                maximum_iterations=MAX_SENTENCE_LENGTH)

                self.decoder_logits_eval = tf.identity(decoder_outputs.rnn_output)
                self.decoder_predict_eval = tf.argmax(self.decoder_logits_eval, axis=-1, name='decoder_pred_eval')
                print(self.decoder_predict_eval)

    def build_optimizer(self, lr):

        optimizer = tf.train.GradientDescentOptimizer(lr)
        trainable_params = tf.trainable_variables()
        gradients = tf.gradients(self.train_loss, trainable_params)
        gradients, _ = tf.clip_by_global_norm(gradients, 5.0)
        gradients = list(zip(gradients, trainable_params))
        self.train_op = optimizer.apply_gradients(gradients)

        for grad, var in gradients:
            self.hist_summary.append(tf.summary.histogram(var.name + '/gradient', grad))
        
        self.hist_summary.append(self.train_summary)
        self.train_summary = tf.summary.merge(self.hist_summary)

    def train(self, sess, batch, print_pred, summary_writer, add_global, prob):

        feed_dict = {self.encoder_inputs: batch.encoder_inputs,
                      self.encoder_inputs_length: batch.encoder_inputs_length,
                      self.decoder_targets: batch.decoder_targets,
                      self.decoder_targets_length: batch.decoder_targets_length,
                      self.batch_size: len(batch.encoder_inputs),
                      self.sampling_prob: prob}

        if print_pred:
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            _, loss, pred, summary, current_step, print_lr = sess.run([self.train_op, self.train_loss, 
                self.decoder_predict_train, self.train_summary, add_global, self.lr], 
                feed_dict=feed_dict, options=run_options)

            i = np.random.randint(0, len(batch.encoder_inputs))
            util.decoder_print(self.idx2word, batch.encoder_inputs[i], batch.encoder_inputs_length[i],
                batch.decoder_targets[i], batch.decoder_targets_length[i], pred[i])
            summary_writer.add_summary(summary, global_step=current_step)
        else:
            _, loss, current_step, print_lr = sess.run([self.train_op, self.train_loss, 
                add_global, self.lr], feed_dict=feed_dict)
        return loss, calc_perplexity(loss), current_step, print_lr
    def eval(self, sess, batch, summary_writer, current_step):

        feed_dict = {self.encoder_inputs: batch.encoder_inputs,
                      self.encoder_inputs_length: batch.encoder_inputs_length,
                      self.decoder_targets: batch.decoder_targets,
                      self.decoder_targets_length: batch.decoder_targets_length,
                      self.batch_size: len(batch.encoder_inputs)}
        loss, pred, summary = sess.run([self.eval_loss, 
            self.decoder_predict_eval, self.eval_summary], feed_dict=feed_dict)
        
        print_num = 3
        print_more = random.sample(range(len(batch.encoder_inputs)), print_num)
        #repeat bug: np.random.randint(len(batch.encoder_inputs), size=(print_num))
        for i in print_more:
            util.decoder_print(self.idx2word, batch.encoder_inputs[i], batch.encoder_inputs_length[i],
                batch.decoder_targets[i], batch.decoder_targets_length[i], pred[i])
        summary_writer.add_summary(summary, global_step=current_step)

        return loss, calc_perplexity(loss)

    def inference(self, sess, batch, txt):
        feed_dict = {self.encoder_inputs: batch.encoder_inputs,
                      self.encoder_inputs_length: batch.encoder_inputs_length,
                      self.batch_size: len(batch.encoder_inputs)}
        pred = sess.run([self.decoder_predict_eval], feed_dict=feed_dict)
        pred = pred[0]
        for i in range(len(pred)):
            sent = util.decoder_inference(self.idx2word, batch.encoder_inputs[i], 
                batch.encoder_inputs_length[i], pred[i])
            txt.write(sent + "\n")


def calc_perplexity(loss):
    return math.exp(float(loss)) if loss < 300 else float('inf')

def train():
    datasetTrain = DatasetTrain()
    print('start build dict...')
    train_data, eval_data = datasetTrain.build_dict(FLAGS.data_dir, filename, 
        FLAGS.min_counts, train_line_num, eval_line_num, emb_size, PKL_EXIST)
    print('build dict done!')
    datasetTrain.prep(train_data)
    datasetEval = DatasetEval()
    datasetEval.load_dict()
    datasetEval.prep(eval_data)

    word2vec_model = datasetTrain.model
    embeddings = np.zeros([datasetTrain.vocab_num, emb_size],dtype=np.float32)
    for word in word2vec_model.wv.vocab:
        index = word2vec_model.wv.vocab[word].index
        vec = word2vec_model.wv[word]
        embeddings[index] = vec
        if word in special_tokens_to_word:
            print('special word: ', word, ', index: ', index)
    pad = np.random.normal(size=[emb_size])
    unk = np.random.normal(size=[emb_size])
    embeddings[0] = pad
    embeddings[3] = unk
    #print(embeddings)

    # np.save('embeddings.npy', embeddings)
    train_graph = tf.Graph()
    eval_graph = tf.Graph()

    gpu_config = tf.ConfigProto()
    gpu_config.gpu_options.allow_growth = True
    
    print('start building train graph...')
    with train_graph.as_default():
        global_step = tf.Variable(0, trainable=False)
        lr = tf.train.exponential_decay(FLAGS.learning_rate,
                    global_step=global_step,
                    decay_steps=3250, decay_rate=0.9, staircase=True)
        add_global = global_step.assign_add(1)
        model = Seq2Seq(voc=datasetTrain.vocab_num, idx2word=datasetTrain.idx2word,
            mode=modes['train'], att=FLAGS.with_attention, lr=lr)
        model.build_model(embeddings)
        model.build_optimizer(FLAGS.learning_rate)
        model.saver = tf.train.Saver(max_to_keep = 3)


        init = tf.global_variables_initializer()
    train_sess = tf.Session(graph=train_graph, config=gpu_config)

    print('start building eval graph...')
    with eval_graph.as_default():
        model_eval = Seq2Seq(voc=datasetEval.vocab_num, idx2word=datasetEval.idx2word,
            mode=modes['eval'], att=FLAGS.with_attention)
        model_eval.build_model(embeddings)
        model_eval.saver = tf.train.Saver(max_to_keep = 3)
        #init_eval = tf.global_variables_initializer()
    eval_sess = tf.Session(graph=eval_graph, config=gpu_config)


    ckpt = tf.train.get_checkpoint_state(FLAGS.save_dir)
    if FLAGS.load_saver and ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
        print('Reloading model parameters..')
        model.saver.restore(train_sess, ckpt.model_checkpoint_path)
        #model_eval.saver.restore(eval_sess, ckpt.model_checkpoint_path)
        print(ckpt.model_checkpoint_path)
    else:
        print('Created new model parameters..')
        train_sess.run(init)
        #eval_sess.run(init_eval)
    ckpts_path = FLAGS.save_dir + "chatbot.ckpt"

    summary_writer = tf.summary.FileWriter(FLAGS.log_dir + '/train')
    summary_writer.add_graph(train_graph)
    summary_writer.add_graph(eval_graph)
    
    num_steps = int( len(datasetTrain.data) / FLAGS.batch_size )
    pbar = tqdm(range(FLAGS.num_epochs))
    
    pt = 0
    total_samp = FLAGS.num_epochs * 3
    samp_prob = util.inv_sigmoid(total_samp)
    current_step = 0 # after first round, assign global_step to current_step
    for epo in pbar:
        print('start: epoch ' + str(epo))
        for i in range(num_steps):
            batch = datasetTrain.next_batch(FLAGS.batch_size, shuffle=True)
            print_pred = False
            if current_step % FLAGS.num_display_steps == 0 and current_step != 0:
                print_pred = True
            loss, perp, current_step, print_lr = model.train(train_sess, batch, print_pred, 
                    summary_writer, add_global, samp_prob[pt])
            if current_step % FLAGS.num_saver_steps == 0 and current_step != 0:
                
                
                ckpt_path = model.saver.save(train_sess, ckpts_path, global_step=current_step)
                print("\nSaver saved: " + ckpt_path)
                model_eval.saver.restore(eval_sess, ckpt_path)
                print("\n[Eval. Prediction] Epoch " + str(epo) + ", step " + str(i) + "/" \
                    + str(num_steps) + "......")
                batch_eval = datasetEval.next_batch(FLAGS.batch_size, shuffle=True)
                loss_eval, perp_eval = model_eval.eval(eval_sess, batch_eval, summary_writer, current_step)
                print("Epoch " + str(epo) + ", step " + str(i) + "/" + str(num_steps) + \
                 ", (Evaluation Loss: " + "{:.4f}".format(loss_eval) + \
                 ", Perplexity: " + "{:.4f}".format(perp_eval) + ")")
            pbar.set_description("S " + str(i) + "/" + \
                    str(num_steps) + "(" + str(current_step) + ")" + \
                    ", (L: " + "{:.4f}".format(loss) + ", P: " + "{:.1f}".format(perp) + ", Samp: "+ \
                            "{:.4f}".format(samp_prob[pt]) + ", lr: "+ "{:.6f}".format(print_lr) + ")" )

            if i % int(num_steps / 3) == 0 and i != 0:
                pt += 1
                print('sampling pt: ' + str( pt ) + '/' + str(total_samp))
        print('sampling pt: ' + str( pt ) + '/' + str(total_samp))


def test():
    datasetTest = DatasetTest()
    print('start build dict...')
    datasetTest.load_dict()
    print('build dict done!')
    test_data = datasetTest.load_test_line(FLAGS.test_dir, FLAGS.test_file)
    datasetTest.prep(test_data)
    embeddings = np.zeros([datasetTest.vocab_num, emb_size],dtype=np.float32)


    test_graph = tf.Graph()
    gpu_config = tf.ConfigProto()
    gpu_config.gpu_options.allow_growth = True
    print('start building test graph...')
    with test_graph.as_default():
        model_test = Seq2Seq(voc=datasetTest.vocab_num, idx2word=datasetTest.idx2word,
            mode=modes['test'], att=FLAGS.with_attention)
        model_test.build_model(embeddings)
        model_test.saver = tf.train.Saver(max_to_keep = 3)
        

    test_sess = tf.Session(graph=test_graph, config=gpu_config)

    ckpt = tf.train.get_checkpoint_state(FLAGS.save_dir)
    if FLAGS.load_saver and ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
        print('Reloading model parameters..')
        model_test.saver.restore(test_sess, ckpt.model_checkpoint_path)
        print(ckpt.model_checkpoint_path)
    else:
        print('ERROR: you should load model for testing!')
        exit(0)
    ckpts_path = FLAGS.save_dir + "chatbot.ckpt"

    tf.train.write_graph(test_sess.graph_def, "./load", "test.pb", False) #proto
    #exit(0)
    num_steps = int( len(datasetTest.test_data) / FLAGS.batch_size )
    txt = open(FLAGS.output_filename, 'w')
    for i in range(num_steps+1):
        batch = datasetTest.next_batch(FLAGS.batch_size)
        model_test.inference(test_sess, batch, txt)
        print(str(i) + '/' + str(num_steps) + ' steps...done')
    print('\n\nTesting finished.')
    print('\nSave file: ' + FLAGS.output_filename)
    txt.close()

def main(_):
  if FLAGS.test_mode == False:
    print('train mode: start')
    train()
  else:
    if FLAGS.load_saver == True:
      print('load saver!!')
    else:
      print('ERROR: you cannot run test without saver...')
      exit(0)
    print('test mode: start')
    test()

if __name__ == '__main__':
 
    parser = argparse.ArgumentParser()
    parser.add_argument('-lr', '--learning_rate', type=float, default=0.005) 
    parser.add_argument('-mi', '--min_counts', type=int, default=100)
    parser.add_argument('-e', '--num_epochs', type=int, default=30)
    parser.add_argument('-b', '--batch_size', type=int, default=250)
    parser.add_argument('-t', '--test_mode', type=bool, default=False)
    parser.add_argument('-d', '--num_display_steps', type=int, default=120)
    parser.add_argument('-ns', '--num_saver_steps', type=int, default=500)
    parser.add_argument('-s', '--save_dir', type=str, default='save/')
    parser.add_argument('-l', '--log_dir', type=str, default='logs/')
    parser.add_argument('-o', '--output_filename', type=str, default='output.txt')
    parser.add_argument('-lo', '--load_saver', type=int, default=0)
    parser.add_argument('-at', '--with_attention', type=int, default=1)
    parser.add_argument('--data_dir', type=str, 
        default=('/project/ntulee/b05901019/Untitled')
    )
    parser.add_argument('--test_dir', type=str, 
        default=('/project/ntulee/b05901019/Untitled')
    )
    parser.add_argument('--test_file', type=str, 
        default=('/test_input.txt')
    )

 
    FLAGS, unparsed = parser.parse_known_args()
    
    tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)
