import logging

import os
from multiprocessing.pool import Pool

import sys
import cv2
import datetime
import fire
import numpy as np
import tensorflow as tf
from skimage.exposure import exposure
from tqdm import tqdm
from imgaug import augmenters as iaa

from checkmate.checkmate import BestCheckpointSaver, get_best_checkpoint
from data_feeder import batch_to_multi_masks, CellImageData, master_dir_test, master_dir_train, \
    CellImageDataManagerValid
from hyperparams import HyperParams
from network import Network
from network_basic import NetworkBasic
from network_unet import NetworkUnet
from network_fusionnet import NetworkFusionNet
from network_unet_valid import NetworkUnetValid
from submission import KaggleSubmission, get_multiple_metric

logger = logging.getLogger('train')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
ch.setFormatter(formatter)
logger.handlers = []
logger.addHandler(ch)

thr_list = np.arange(0.5, 1.0, 0.05)


class Trainer:
    def validate(self, model, checkpoint, tag=''):
        self.run(model, epoch=0, tag=tag, checkpoint=checkpoint, save_result=True)

    def run(self, model, epoch=600,
            batchsize=16, learning_rate=0.0001, early_rejection=False,
            valid_interval=10, tag='', show_train=0, show_valid=0, show_test=0, save_result=True, checkpoint='',
            **kwargs):
        if model == 'basic':
            network = NetworkBasic(batchsize, unet_weight=True)
        elif model == 'simple_unet':
            network = NetworkUnet(batchsize, unet_weight=True)
        elif model == 'unet':
            network = NetworkUnetValid(batchsize, unet_weight=True)
        elif model == 'simple_fusion':
            network = NetworkFusionNet(batchsize)
        else:
            raise Exception('model name(%s) is not valid' % model)

        logger.info('constructing network model: %s' % model)

        ds_train, ds_valid, ds_valid_full, ds_test = network.get_input_flow()
        network.build()

        net_output = network.get_output()
        net_loss = network.get_loss()

        global_step = tf.Variable(0, trainable=False)
        learning_rate_v, train_op = network.get_optimize_op(global_step=global_step,
                                                            learning_rate=learning_rate)

        logger.info('constructed-')

        best_loss_val = 999999
        best_miou_val = 0.0
        name = '%s_%s_lr=%.4f_epoch=%d_bs=%d' % (
            tag if tag else datetime.datetime.now().strftime("%y%m%dT%H%M%f"),
            model,
            learning_rate,
            epoch,
            batchsize,
        )
        model_path = os.path.join(KaggleSubmission.BASEPATH, name, 'model')
        best_ckpt_saver = BestCheckpointSaver(
            save_dir=model_path,
            num_to_keep=100,
            maximize=True
        )

        saver = tf.train.Saver()
        config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        m_epoch = 0
        with tf.Session(config=config) as sess:
            logger.info('training started+')
            if not checkpoint:
                sess.run(tf.global_variables_initializer())
            else:
                saver.restore(sess, checkpoint)
                logger.info('restored from checkpoint, %s' % checkpoint)

            try:
                losses = []
                for e in range(epoch):
                    loss_val_avg = []
                    train_cnt = 0
                    for dp_train in ds_train.get_data():
                        _, loss_val = sess.run(
                            [train_op, net_loss],
                            feed_dict=network.get_feeddict(dp_train, True)
                        )
                        loss_val_avg.append(loss_val)
                        train_cnt += 1
                        # for debug
                        # cv2.imshow('train', Network.visualize(dp_train[0][0], dp_train[2][0], None, dp_train[3][0], 'norm1'))
                        # cv2.waitKey(0)

                    step, lr = sess.run([global_step, learning_rate_v])
                    loss_val_avg = sum(loss_val_avg) / len(loss_val_avg)
                    logger.info('training %d epoch %d step, lr=%.8f loss=%.4f train_iter=%d' % (
                    e + 1, step, lr, loss_val_avg, train_cnt))
                    losses.append(loss_val)

                    if early_rejection and len(losses) > 100 and losses[len(losses) - 100] * 1.05 < loss_val_avg:
                        logger.info('not improved, stop at %d' % e)
                        break

                    # early rejection
                    if early_rejection and ((e == 50 and loss_val > 0.5) or (e == 200 and loss_val > 0.2)):
                        logger.info('not improved training loss, stop at %d' % e)
                        break

                    m_epoch = e
                    avg = 10.0
                    if loss_val < 0.20 and (e + 1) % valid_interval == 0:
                        avg = []
                        for _ in range(5):
                            for dp_valid in ds_valid.get_data():
                                loss_val = sess.run(
                                    net_loss,
                                    feed_dict=network.get_feeddict(dp_valid, False)
                                )
                                avg.append(loss_val)

                        avg = sum(avg) / len(avg)
                        logger.info('validation loss=%.4f' % (avg))
                        if best_loss_val > avg:
                            best_loss_val = avg

                    if avg < 0.16 and e > 50 and (e + 1) % valid_interval == 0:
                        cnt_tps = np.array((len(thr_list)), dtype=np.int32),
                        cnt_fps = np.array((len(thr_list)), dtype=np.int32)
                        cnt_fns = np.array((len(thr_list)), dtype=np.int32)
                        pool_args = []
                        for idx, dp_valid in tqdm(enumerate(ds_valid_full.get_data()), desc='validate using the iou metric', total=len(CellImageDataManagerValid.LIST + CellImageDataManagerValid.LIST_EXT1)):
                            image = dp_valid[0]
                            instances = network.inference(sess, image)
                            pool_args.append((thr_list, instances, dp_valid[2]))

                        pool = Pool(processes=32)
                        cnt_results = pool.map(do_get_multiple_metric, pool_args)
                        pool.close()
                        pool.join()
                        for cnt_result in cnt_results:
                            cnt_tps = cnt_tps + cnt_result[0]
                            cnt_fps = cnt_fps + cnt_result[1]
                            cnt_fns = cnt_fns + cnt_result[2]

                        ious = np.divide(cnt_tps, cnt_tps + cnt_fps + cnt_fns)
                        mIou = np.mean(ious)
                        logger.info('validation metric: %.5f' % mIou)
                        if best_miou_val < mIou:
                            best_miou_val = mIou
                        best_ckpt_saver.handle(mIou, sess, global_step)  # save & keep best model

                        # early rejection by mIou
                        if early_rejection and e > 50 and best_miou_val < 0.15:
                            break
                        if early_rejection and e > 100 and best_miou_val < 0.25:
                            break
            except KeyboardInterrupt:
                logger.info('interrupted. stop training, start to validate.')

            try:
                chk_path = get_best_checkpoint(model_path, select_maximum_value=True)
                logger.info('training is done. Start to evaluate the best model. %s' % chk_path)
                saver.restore(sess, chk_path)
            except Exception as e:
                logger.warning('error while loading the best model:' + str(e))

            # show sample in train set : show_train > 0
            for idx, dp_train in enumerate(ds_train.get_data()):
                if idx >= show_train:
                    break
                image = dp_train[0][0]
                instances = network.inference(sess, image)

                cv2.imshow('train', Network.visualize(image, dp_train[2][0], instances, None))
                cv2.waitKey(0)

            # show sample in valid set : show_valid > 0
            kaggle_submit = KaggleSubmission(name)
            logger.info('Start to test on validation set.... (may take a while)')
            valid_metrics = []
            for idx, dp_valid in tqdm(enumerate(ds_valid_full.get_data()), total=len(CellImageDataManagerValid.LIST)):
                image = dp_valid[0]
                labels = list(batch_to_multi_masks(dp_valid[2], transpose=False))
                instances = network.inference(sess, image)

                tp, fp, fn = get_multiple_metric(thr_list, instances, labels)

                img_vis = Network.visualize(image, labels, instances, None)
                score = (np.mean(tp / (tp + fp + fn)))
                kaggle_submit.save_valid_image(str(idx), img_vis, score=score)
                valid_metrics.append(score)

                if idx < show_valid:
                    logger.info('score=%.3f, tp=%d, fp=%d, fn=%d' % (tp / (tp + fp + fn), tp, fp, fn))
                    cv2.imshow('valid', Network.visualize(image, dp_valid[2], instances, None))
                    cv2.waitKey(0)
            logger.info('validation ends. score=%.4f' % np.mean(valid_metrics))

            # show sample in test set
            logger.info('saving...')
            if save_result:
                for idx, dp_test in enumerate(ds_test.get_data()):
                    image = dp_test[0]
                    test_id = dp_test[1][0]
                    img_h, img_w = dp_test[2][0], dp_test[2][1]
                    assert img_h > 0 and img_w > 0, '%d %s' % (idx, test_id)
                    instances = network.inference(sess, image)

                    img_vis = Network.visualize(image, None, instances, None)
                    if idx < show_test:
                        cv2.imshow('test', img_vis)
                        cv2.waitKey(0)

                    # save to submit
                    instances = Network.resize_instances(instances, (img_h, img_w))
                    kaggle_submit.save_image(test_id, img_vis)
                    kaggle_submit.add_result(test_id, instances)
                kaggle_submit.save()
        logger.info(
            'done. epoch=%d best_loss_val=%.4f best_mIOU=%.4f name= %s' % (m_epoch, best_loss_val, best_miou_val, name))
        return best_miou_val, name

    def single_id(self, model, checkpoint, single_id, set_type='train'):
        batchsize = 1
        if model == 'basic':
            network = NetworkBasic(batchsize, unet_weight=True)
        elif model == 'simple_unet':
            network = NetworkUnet(batchsize, unet_weight=True)
        elif model == 'unet':
            network = NetworkUnetValid(batchsize, unet_weight=True)
        elif model == 'simple_fusion':
            network = NetworkFusionNet(batchsize)
        else:
            raise Exception('model name(%s) is not valid' % model)

        logger.info('constructing network model: %s' % model)
        network.build()

        saver = tf.train.Saver()
        config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        with tf.Session(config=config) as sess:
            saver.restore(sess, checkpoint)
            logger.info('restored from checkpoint, %s' % checkpoint)

            d = CellImageData(single_id, (master_dir_train if set_type == 'train' else master_dir_test))
            h, w = d.img.shape[:2]
            logger.info('image size=(%d x %d)' % (w, h))
            # n = iaa.ContrastNormalization(alpha=0.8)
            # d.img = n.augment_image(d.img)

            d = network.preprocess(d)

            image = d.image(is_gray=False)

            labels = list(d.multi_masks(transpose=False))
            instances = network.inference(sess, image)

            # re-inference after rescale image

            # resize as the original
            image = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
            instances = Network.resize_instances(instances, target_size=(h, w))
            labels = Network.resize_instances(labels, target_size=(h, w))

            tp, fp, fn = get_multiple_metric(thr_list, instances, labels)

            img_vis = Network.visualize(image, labels, instances, None)

            logger.info('instances=%d, labels=%d' % (len(instances), len(labels)))
            for i, thr in enumerate(thr_list):
                logger.info('score=%.3f, tp=%d, fp=%d, fn=%d --- iou %.2f' % (
                    (tp / (tp + fp + fn))[i],
                    tp[i],
                    fp[i],
                    fn[i],
                    thr
                ))
            logger.info('score=%.3f, tp=%.1f, fp=%.1f, fn=%.1f --- mean' % (
                np.mean(tp / (tp + fp + fn)),
                np.mean(tp),
                np.mean(fp),
                np.mean(fn)
            ))
            cv2.imshow('valid', img_vis)
            cv2.waitKey(0)


def do_get_multiple_metric(args):
    thr_list, instances, multi_masks_batch = args
    label = batch_to_multi_masks(multi_masks_batch, transpose=False)
    return get_multiple_metric(thr_list, instances, label)


if __name__ == '__main__':
    fire.Fire(Trainer)
    print(HyperParams.get().__dict__)
