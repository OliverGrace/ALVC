import argparse
import numpy as np
import tensorflow as tf
import tensorflow_compression as tfc
import os
from scipy import misc
import CNN_recurrent
import motion
import functions
import helper2
import flow_vis

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

config = tf.ConfigProto(allow_soft_placement=True)
sess = tf.Session(config=config)

parser = argparse.ArgumentParser(
      formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--path", default='BasketballPass')
parser.add_argument("--frame", type=int, default=100)
parser.add_argument("--f_P", type=int, default=5)
parser.add_argument("--inter", type=int, default=2)
parser.add_argument("--b_P", type=int, default=5)
parser.add_argument("--mode", default='PSNR', choices=['PSNR'])
parser.add_argument("--metric", default='PSNR', choices=['PSNR'])
parser.add_argument("--VTM", type=int, default=1, choices=[0, 1])
parser.add_argument("--l", type=int, default=512, choices=[256, 512, 1024, 2048])
parser.add_argument("--N", type=int, default=128, choices=[128])
parser.add_argument("--M", type=int, default=128, choices=[128])

args = parser.parse_args()

# Settings
I_level, Height, Width, batch_size, Channel, \
activation, GOP_size, GOP_num, \
path, path_com, path_bin, path_lat = helper2.configure(args)

if os.path.exists(path_com + '/extra_states'):
    os.system('rm -r ' + path_com + '/extra_states')

# Placeholder
Y0_com_tensor = tf.placeholder(tf.float32, [batch_size, Height, Width, Channel]) # reference frame
Y1_raw_tensor = tf.placeholder(tf.float32, [batch_size, Height, Width, Channel]) # raw frame to compress

hidden_states = tf.placeholder(tf.float32, [8, batch_size, Height//4, Width//4, args.N]) # hidden states in RAE

c_enc_mv, h_enc_mv, \
c_dec_mv, h_dec_mv, \
c_enc_res, h_enc_res, \
c_dec_res, h_dec_res = tf.split(hidden_states, 8, axis=0)

RPM_flag = tf.placeholder(tf.bool, []) # use RPM (=1) or bottleneck (=0)

# motion estimation
with tf.variable_scope("flow_motion"):
    motion_tensor, _, _, _, _, _ = motion.optical_flow(Y0_com_tensor, Y1_raw_tensor, batch_size, Height, Width)

# RAE encoder for motion
motion_latent, c_enc_mv_out, h_enc_mv_out = CNN_recurrent.MV_analysis(motion_tensor, num_filters=args.N, out_filters=args.M,
                               Height=Height, Width=Width,
                               c_state=c_enc_mv[0], h_state=h_enc_mv[0], act=activation)

# encode the latent of the first P frame by the bottleneck
entropy_bottleneck = tfc.EntropyBottleneck(name='entropy_bottleneck')
string = tf.squeeze(entropy_bottleneck.compress(motion_latent), axis=0)
motion_latent_decom = entropy_bottleneck.decompress(tf.expand_dims(string, 0), [Height//16, Width//16, args.M], channels=args.M)
motion_latent_hat = tf.cond(RPM_flag, lambda: tf.round(motion_latent), lambda: motion_latent_decom)

# RAE decoder for motion
motion_hat, c_dec_mv_out, h_dec_mv_out = CNN_recurrent.MV_synthesis(motion_latent_hat, num_filters=args.N,
                                      Height=Height, Width=Width,
                                      c_state=c_dec_mv[0], h_state=h_dec_mv[0], act=activation)

# Motion Compensation
Y1_warp = tf.contrib.image.dense_image_warp(Y0_com_tensor, motion_hat)
# Y1_warp = warp.dense_image_warp(Y0_com_tensor, motion_hat)

MC_input = tf.concat([motion_hat, Y0_com_tensor, Y1_warp], axis=-1)
Y1_MC = functions.MC_RLVC(MC_input)

# Get residual
Res = Y1_raw_tensor - Y1_MC

# RAE encoder for residual
res_latent, c_enc_res_out, h_enc_res_out = CNN_recurrent.Res_analysis(Res, num_filters=args.N, out_filters=args.M,
                               Height=Height, Width=Width,
                               c_state=c_enc_res[0], h_state=h_enc_res[0], act=activation)

# encode the latent of the first P frame by the bottleneck
entropy_bottleneck2 = tfc.EntropyBottleneck(name='entropy_bottleneck_1_1')
string2 = entropy_bottleneck2.compress(res_latent)
string2 = tf.squeeze(string2, axis=0)
res_latent_decom = entropy_bottleneck2.decompress(tf.expand_dims(string2, 0), [Height//16, Width//16, args.M], channels=args.M)
res_latent_hat = tf.cond(RPM_flag, lambda: tf.round(res_latent), lambda: res_latent_decom)

# RAE decoder for residual
res_hat, c_dec_res_out, h_dec_res_out = CNN_recurrent.Res_synthesis(res_latent_hat, num_filters=args.N,
                                      Height=Height, Width=Width,
                                      c_state=c_dec_res[0], h_state=h_dec_res[0], act=activation)

# reconstructed frame
Y1_decoded = tf.clip_by_value(res_hat + Y1_MC, 0, 1)

# output hidden states
hidden_states_out = tf.stack([c_enc_mv_out, h_enc_mv_out,
                               c_dec_mv_out, h_dec_mv_out,
                               c_enc_res_out, h_enc_res_out,
                               c_dec_res_out, h_dec_res_out], axis=0)

# PANR or MS-SSIM
# if args.metric == 'PSNR':
mse = tf.reduce_mean(tf.squared_difference(Y1_decoded, Y1_raw_tensor))
quality_tensor = 10.0*tf.log(1.0/mse)/tf.log(10.0)
mse_mc = tf.reduce_mean(tf.squared_difference(Y1_MC, Y1_raw_tensor))
psnr_mc = 10.0 * tf.log(1.0 / mse_mc) / tf.log(10.0)
# elif args.metric == 'MS-SSIM':
#     quality_tensor = tf.math.reduce_mean(tf.image.ssim_multiscale(Y1_decoded, Y1_raw_tensor, max_val=1))
#     mse_mc = tf.reduce_mean(tf.squared_difference(Y1_MC, Y1_raw_tensor))
#     psnr_mc = 10.0 * tf.log(1.0 / mse_mc) / tf.log(10.0)

# load model
saver = tf.train.Saver(max_to_keep=None)
model_path = './model/Extrapolation/lambda_' + str(args.l) + '_extra'
saver.restore(sess, save_path=model_path + '/model.ckpt-150000')


# init quality
quality_frame = np.zeros([args.frame])

# encode the first I frame
frame_index = 1
quality = helper2.encode_I(args, frame_index, I_level, path, path_com, path_bin)
quality_frame[frame_index - 1] = quality

if os.path.exists(path_com + '/extra_states'):
    os.system('rm -r ' + path_com + '/extra_states')

# encode GOPs
for g in range(GOP_num):

    np.save(path_bin + 'quality.npy', quality_frame)

    # forward P frames

    # load I frame (compressed)
    frame_index = g * GOP_size + 1
    F0_com = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '.png')
    F0_com = np.expand_dims(F0_com, axis=0)

    for f in range(args.f_P):

        # load P frame (raw)
        frame_index = g * GOP_size + f + 2
        F1_raw = misc.imread(path + 'output_' + str(frame_index).zfill(4) + '.png')
        F1_raw = np.expand_dims(F1_raw, axis=0)

        # init hidden states
        if f % 5 == 0:
            h_state = np.zeros([8, batch_size, Height // 4, Width // 4, args.N], dtype=np.float)
            # since the model is optimized on 6 frames, we reset hidden states every 6 P frames

        if f == 0:
            flag = False
            # the first P frame uses bottleneck
        else:
            flag = True

        if f >= 1:
            # python_cpu = ''
            os.system(
                'python Extrapolation.py --path ' + path_com + ' --idx ' + str(frame_index) + ' --l ' + str(args.l))
            F_ref = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '_extra.png').astype(float)
            F_ref = np.expand_dims(F_ref, axis=0)
            # F_ref = np.load(path_com + 'output_' + str(frame_index).zfill(4) + '_extra.npy') * 255.0
            mse = np.mean(np.power(np.subtract(F_ref / 255.0, F1_raw / 255.0), 2.0))
            quality = 10 * np.log10(1.0 / mse)

            # print('Frame', frame_index, args.metric + '_extra =', quality)

        else:
            F_ref = F0_com

        # run RAE
        F0_com, string_MV, string_Res, quality, h_state, latent_mv, latent_res, psnr_mc_value, motion_map \
            = sess.run([Y1_decoded, string, string2, quality_tensor,
             hidden_states_out, motion_latent_hat, res_latent_hat, psnr_mc, motion_tensor],
            feed_dict={Y0_com_tensor: F_ref / 255.0, Y1_raw_tensor: F1_raw / 255.0,
                       hidden_states: h_state, RPM_flag: flag})
        F0_com = F0_com * 255
        # flow_color = flow_vis.flow_to_color(motion_map[0], convert_to_bgr=False)
        # save bottleneck bitstream
        if not flag:
            with open(path_bin + '/output_' + str(frame_index).zfill(4) + '.bin', "wb") as ff:
                ff.write(np.array(len(string_MV), dtype=np.uint16).tobytes())
                ff.write(string_MV)
                ff.write(string_Res)

        # save compressed frame and latents
        misc.imsave(path_com + '/output_' + str(frame_index).zfill(4) + '.png', np.uint8(np.round(F0_com[0])))
        # misc.imsave(path_com + '/output_' + str(frame_index).zfill(4) + '_flow.png', flow_color)
        np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_mv.npy', latent_mv)
        np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_res.npy', latent_res)

        quality_frame[frame_index - 1] = quality

        print('Frame', frame_index, args.metric + ' =', quality)

    os.system('rm -r ' + path_com + '/extra_states')

    # encode the next I frame
    frame_index = (g + 1) * GOP_size + 1
    quality = helper2.encode_I(args, frame_index, I_level, path, path_com, path_bin)
    quality_frame[frame_index - 1] = quality

    # backward P frames

    # load I frame (compressed)
    F0_com = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '.png')
    F0_com = np.expand_dims(F0_com, axis=0)

    for f in range(args.b_P):

        # load P frame (raw)
        frame_index = (g + 1) * GOP_size - f
        F1_raw = misc.imread(path + 'output_' + str(frame_index).zfill(4) + '.png')
        F1_raw = np.expand_dims(F1_raw, axis=0)

        # init hidden states
        if f % 5 == 0:
            h_state = np.zeros([8, batch_size, Height // 4, Width // 4, args.N], dtype=np.float)
            # since the model is optimized on 6 frames, we reset hidden states every 6 P frames

        if f == 0:
            flag = False
            # the first P frame uses bottleneck
        else:
            flag = True

        if f >= 1:
            # python_cpu = ''
            os.system(
                'python Extrapolation.py --dirc bw --path ' + path_com + ' --idx ' + str(frame_index) + ' --l ' + str(args.l))
            F_ref = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '_extra.png').astype(float)
            F_ref = np.expand_dims(F_ref, axis=0)

            mse = np.mean(np.power(np.subtract(F_ref / 255.0, F1_raw / 255.0), 2.0))
            quality = 10 * np.log10(1.0 / mse)

            # print('Frame', frame_index, args.metric + '_extra =', quality)

        else:
            F_ref = F0_com

        # run RAE
        F0_com, string_MV, string_Res, quality, h_state, latent_mv, latent_res, psnr_mc_value, motion_map \
            = sess.run([Y1_decoded, string, string2, quality_tensor,
                        hidden_states_out, motion_latent_hat, res_latent_hat, psnr_mc, motion_tensor],
                       feed_dict={Y0_com_tensor: F_ref / 255.0, Y1_raw_tensor: F1_raw / 255.0,
                                  hidden_states: h_state, RPM_flag: flag})
        F0_com = F0_com * 255

        # flow_color = flow_vis.flow_to_color(motion_map[0], convert_to_bgr=False)

        # save bottleneck bitstream
        if not flag:
            with open(path_bin + '/output_' + str(frame_index).zfill(4) + '.bin', "wb") as ff:
                ff.write(np.array(len(string_MV), dtype=np.uint16).tobytes())
                ff.write(string_MV)
                ff.write(string_Res)

        # save compressed frame and latents
        misc.imsave(path_com + '/output_' + str(frame_index).zfill(4) + '.png', np.uint8(np.round(F0_com[0])))
        # misc.imsave(path_com + '/output_' + str(frame_index).zfill(4) + '_flow.png', flow_color)
        np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_mv.npy', latent_mv)
        np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_res.npy', latent_res)

        quality_frame[frame_index - 1] = quality

        print('Frame', frame_index, args.metric + ' =', quality)

    os.system('rm -r ' + path_com + '/extra_states')

# encode rest frames
rest_frame_num = args.frame - 1 - GOP_size * GOP_num

# load I frame (compressed)
frame_index = GOP_num * GOP_size + 1
F0_com = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '.png')
F0_com = np.expand_dims(F0_com, axis=0)

for f in range(rest_frame_num):

    # load P frame (raw)
    frame_index = GOP_num * GOP_size + f + 2
    F1_raw = misc.imread(path + 'output_' + str(frame_index).zfill(4) + '.png')
    F1_raw = np.expand_dims(F1_raw, axis=0)

    # init hidden states
    if f % 5 == 0:
        h_state = np.zeros([8, batch_size, Height // 4, Width // 4, args.N], dtype=np.float)
        if os.path.exists(path_com + '/extra_states'):
            os.system('rm -r ' + path_com + '/extra_states')
        # since the model is optimized on 6 frames, we reset hidden states every 6 P frames

    if f == 0:
        flag = False
        # the first P frame uses the bottleneck
    else:
        flag = True


    if f >= 1:
        # python_cpu = ''
        os.system('python Extrapolation.py --path ' + path_com + ' --idx ' + str(frame_index) + ' --l ' + str(args.l))

        F_ref = misc.imread(path_com + 'output_' + str(frame_index).zfill(4) + '_extra.png').astype(float)
        F_ref = np.expand_dims(F_ref, axis=0)

        mse = np.mean(np.power(np.subtract(F_ref / 255.0, F1_raw / 255.0), 2.0))
        quality = 10 * np.log10(1.0 / mse)

        # print('Frame', frame_index, args.metric + '_extra =', quality)

    else:
        F_ref = F0_com


    # run RAE
    F0_com, string_MV, string_Res, quality, h_state, latent_mv, latent_res, psnr_mc_value, res_value \
        = sess.run([Y1_decoded, string, string2, quality_tensor,
                    hidden_states_out, motion_latent_hat, res_latent_hat, psnr_mc, Res],
                   feed_dict={Y0_com_tensor: F_ref / 255.0, Y1_raw_tensor: F1_raw / 255.0,
                              hidden_states: h_state, RPM_flag: flag})
    F0_com = F0_com * 255
    # save bottleneck bitstream
    if not flag:
        with open(path_bin + '/output_' + str(frame_index).zfill(4) + '.bin', "wb") as ff:
            ff.write(np.array(len(string_MV), dtype=np.uint16).tobytes())
            ff.write(string_MV)
            ff.write(string_Res)

    # save compressed frame and latents
    misc.imsave(path_com + '/output_' + str(frame_index).zfill(4) + '.png', np.uint8(np.round(F0_com[0])))
    np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_mv.npy', latent_mv)
    np.save(path_lat + '/output_' + str(frame_index).zfill(4) + '_res.npy', latent_res)

    quality_frame[frame_index - 1] = quality

    print('Frame', frame_index, args.metric + ' =', quality)

if os.path.exists(path_com + '/extra_states'):
    os.system('rm -r ' + path_com + '/extra_states')

np.save(path_bin + 'quality.npy', quality_frame)

os.system('rm ' + path_com + '/*_extra.png')
os.system('rm ' + path_com + '/*.yuv')






