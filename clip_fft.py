import os
import warnings
warnings.filterwarnings("ignore")
import argparse
import numpy as np
from imageio import imread, imsave
import shutil
from googletrans import Translator, constants

import pywt
from pytorch_wavelets import DWTForward, DWTInverse
from pytorch_wavelets import DTCWTForward, DTCWTInverse

import torch
import torchvision
import torch.nn.functional as F

import clip
os.environ['KMP_DUPLICATE_LIB_OK']='True'
from sentence_transformers import SentenceTransformer
import lpips

from utils import slice_imgs, derivat, basename, img_list, img_read, plot_text, txt_clean, checkout
import transforms
try: # progress bar for notebooks 
    get_ipython().__class__.__name__
    from progress_bar import ProgressIPy as ProgressBar
except: # normal console
    from progress_bar import ProgressBar

clip_models = ['ViT-B/16', 'ViT-B/32', 'RN101', 'RN50x16', 'RN50x4', 'RN50']

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-i',  '--in_img',  default=None, help='input image')
    parser.add_argument('-t',  '--in_txt',  default=None, help='input text')
    parser.add_argument('-t2', '--in_txt2', default=None, help='input text - style')
    parser.add_argument('-t0', '--in_txt0', default=None, help='input text to subtract')
    parser.add_argument(       '--out_dir', default='_out')
    parser.add_argument('-s',  '--size',    default='1280-720', help='Output resolution')
    parser.add_argument('-r',  '--resume',  default=None, help='Path to saved FFT snapshots, to resume from')
    parser.add_argument(       '--fstep',   default=1, type=int, help='Saving step')
    parser.add_argument('-tr', '--translate', action='store_true', help='Translate text with Google Translate')
    parser.add_argument('-ml', '--multilang', action='store_true', help='Use SBERT multilanguage model for text')
    parser.add_argument(       '--save_pt', action='store_true', help='Save FFT snapshots for further use')
    parser.add_argument('-v',  '--verbose', default=True, type=bool)
    # training
    parser.add_argument('-m',  '--model',   default='ViT-B/32', choices=clip_models, help='Select CLIP model to use')
    parser.add_argument(       '--steps',   default=200, type=int, help='Total iterations')
    parser.add_argument(       '--samples', default=200, type=int, help='Samples to evaluate')
    parser.add_argument(       '--lrate',   default=0.05, type=float, help='Learning rate')
    parser.add_argument('-p',  '--prog',    action='store_true', help='Enable progressive lrate growth (up to double a.lrate)')
    # wavelet
    parser.add_argument(       '--dwt',     action='store_true', help='Use DWT instead of FFT')
    parser.add_argument('-w',  '--wave',    default='coif2', help='wavelets: db[1..], coif[1..], haar, dmey')
    # tweaks
    parser.add_argument('-a',  '--align',   default='uniform', choices=['central', 'uniform', 'overscan'], help='Sampling distribution')
    parser.add_argument('-tf', '--transform', action='store_true', help='use augmenting transforms?')
    parser.add_argument(       '--contrast', default=0.9, type=float)
    parser.add_argument(       '--colors',  default=1.5, type=float)
    parser.add_argument(       '--decay',   default=1.5, type=float)
    parser.add_argument('-sh', '--sharp',   default=0.3, type=float)
    parser.add_argument('-mm', '--macro',   default=0.4, type=float, help='Endorse macro forms 0..1 ')
    parser.add_argument('-e',  '--enhance', default=0, type=float, help='Enhance consistency, boosts training')
    parser.add_argument('-n',  '--noise',   default=0, type=float, help='Add noise to suppress accumulation') # < 0.05 ?
    parser.add_argument('-nt', '--notext',  default=0, type=float, help='Subtract typed text as image (avoiding graffiti?), [0..1]')
    parser.add_argument('-c',  '--sync',    default=0, type=float, help='Sync output to input image')
    parser.add_argument(       '--invert',  action='store_true', help='Invert criteria')
    a = parser.parse_args()

    if a.size is not None: a.size = [int(s) for s in a.size.split('-')][::-1]
    if len(a.size)==1: a.size = a.size * 2
    if a.in_img is not None and a.sync > 0: a.align = 'overscan'
    if a.multilang is True: a.model = 'ViT-B/32' # sbert model is trained with ViT
    a.diverse = -a.enhance
    a.expand = abs(a.enhance)
    return a

### FFT from Lucent library ###  https://github.com/greentfrapp/lucent

def to_valid_rgb(image_f, colors=1., decorrelate=True):
    color_correlation_svd_sqrt = np.asarray([[0.26, 0.09, 0.02],
                                             [0.27, 0.00, -0.05],
                                             [0.27, -0.09, 0.03]]).astype("float32")
    color_correlation_svd_sqrt /= np.asarray([colors, 1., 1.]) # saturate, empirical
    max_norm_svd_sqrt = np.max(np.linalg.norm(color_correlation_svd_sqrt, axis=0))
    color_correlation_normalized = color_correlation_svd_sqrt / max_norm_svd_sqrt

    def _linear_decorrelate_color(tensor):
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        t_permute = tensor.permute(0,2,3,1)
        t_permute = torch.matmul(t_permute, torch.tensor(color_correlation_normalized.T).to(device))
        tensor = t_permute.permute(0,3,1,2)
        return tensor

    def inner(*args, **kwargs):
        image = image_f(*args, **kwargs)
        if decorrelate:
            image = _linear_decorrelate_color(image)
        return torch.sigmoid(image)
    return inner
    
def init_dwt(resume=None, shape=None, wave=None, colors=None):
    size = None
    wp_fake = pywt.WaveletPacket2D(data=np.zeros(shape[2:]), wavelet='db1', mode='symmetric')
    xfm = DWTForward(J=wp_fake.maxlevel, wave=wave, mode='symmetric').cuda()
    # xfm = DTCWTForward(J=lvl, biort='near_sym_b', qshift='qshift_b').cuda() # 4x more params, biort ['antonini','legall','near_sym_a','near_sym_b']
    ifm = DWTInverse(wave=wave, mode='symmetric').cuda() # symmetric zero periodization
    # ifm = DTCWTInverse(biort='near_sym_b', qshift='qshift_b').cuda() # 4x more params, biort ['antonini','legall','near_sym_a','near_sym_b']
    if resume is None: # random init
        Yl_in, Yh_in = xfm(torch.zeros(shape).cuda())
        Ys = [torch.randn(*Y.shape).cuda() for Y in [Yl_in, *Yh_in]]
    elif isinstance(resume, str):
        if os.path.isfile(resume):
            if os.path.splitext(resume)[1].lower()[1:] in ['jpg','png','tif','bmp']:
                img_in = imread(resume)
                Ys = img2dwt(img_in, wave=wave, colors=colors)
                print(' loaded image', resume, img_in.shape, 'level', len(Ys)-1)
                size = img_in.shape[:2]
                wp_fake = pywt.WaveletPacket2D(data=np.zeros(size), wavelet='db1', mode='symmetric')
                xfm = DWTForward(J=wp_fake.maxlevel, wave=wave, mode='symmetric').cuda()
            else:
                Ys = torch.load(resume)
                Ys = [y.detach().cuda() for y in Ys]
        else: print(' Snapshot not found:', resume); exit()
    else:
        Ys = [y.cuda() for y in resume]
    # print('level', len(Ys)-1, 'low freq', Ys[0].cpu().numpy().shape)
    return Ys, xfm, ifm, size

def dwt_image(shape, wave='coif2', sharp=0.3, colors=1., resume=None):
    Ys, _, ifm, size = init_dwt(resume, shape, wave, colors)
    Ys = [y.requires_grad_(True) for y in Ys]
    scale = dwt_scale(Ys, sharp)

    def inner(shift=None, contrast=1.):
        image = ifm((Ys[0], [Ys[i+1] * float(scale[i]) for i in range(len(Ys)-1)]))
        image = image * contrast / image.std() # keep contrast, empirical *1.33
        return image

    return Ys, inner, size

def dwt_scale(Ys, sharp):
    scale = []
    [h0,w0] = Ys[1].shape[3:5]
    for i in range(len(Ys)-1):
        [h,w] = Ys[i+1].shape[3:5]
        scale.append( ((h0*w0)/(h*w)) ** (1.-sharp) )
        # print(i+1, Ys[i+1].shape)
    return scale

def img2dwt(img_in, wave='coif2', sharp=0.3, colors=1.):
    if not isinstance(img_in, torch.Tensor):
        img_in = torch.Tensor(img_in).cuda().permute(2,0,1).unsqueeze(0).float() / 255.
    img_in = un_rgb(img_in, colors=colors)
    with torch.no_grad():
        wp_fake = pywt.WaveletPacket2D(data=np.zeros(img_in.shape[2:]), wavelet='db1', mode='zero')
        lvl = wp_fake.maxlevel
        # print(img_in.shape, lvl)
        xfm = DWTForward(J=lvl, wave=wave, mode='symmetric').cuda()
        Yl_in, Yh_in = xfm(img_in.cuda())
        Ys = [Yl_in, *Yh_in]
    scale = dwt_scale(Ys, sharp)
    for i in range(len(Ys)-1):
        Ys[i+1] /= scale[i]
    return Ys

def pixel_image(shape, resume=None, sd=1., *noargs, **nokwargs):
    size = None
    if resume is None:
        tensor = torch.randn(*shape) * sd
    elif isinstance(resume, str):
        if os.path.isfile(resume):
            img_in = imread(resume) / 255.
            tensor = torch.Tensor(img_in).permute(2,0,1).unsqueeze(0).float()
            tensor = un_rgb(tensor-0.5, colors=2.) # experimental
            size = img_in.shape[:2]
            print(resume, size)
        else: print(' Image not found:', resume); exit()
    else:
        if isinstance(resume, list): resume = resume[0]
        tensor = resume
    tensor = tensor.cuda().requires_grad_(True)

    def inner(shift=None, contrast=1.): # *noargs, **nokwargs
        image = tensor * contrast / tensor.std()
        return image
    return [tensor], inner, size # lambda: tensor

# From https://github.com/tensorflow/lucid/blob/master/lucid/optvis/param/spatial.py
def rfft2d_freqs(h, w):
    """Computes 2D spectrum frequencies."""
    fy = np.fft.fftfreq(h)[:, None]
    # when we have an odd input dimension we need to keep one additional frequency and later cut off 1 pixel
    w2 = (w+1)//2 if w%2 == 1 else w//2+1
    fx = np.fft.fftfreq(w)[:w2]
    return np.sqrt(fx * fx + fy * fy)

def resume_fft(resume=None, shape=None, decay=None, colors=1.6, sd=0.01):
    size = None
    if resume is None: # random init
        params_shape = [*shape[:3], shape[3]//2+1, 2] # [1,3,512,257,2] for 512x512 (2 for imaginary and real components)
        params = 0.01 * torch.randn(*params_shape).cuda()
    elif isinstance(resume, str):
        if os.path.isfile(resume):
            if os.path.splitext(resume)[1].lower()[1:] in ['jpg','png','tif','bmp']:
                img_in = imread(resume)
                params = img2fft(img_in, decay, colors)
                size = img_in.shape[:2]
            else:
                params = torch.load(resume)
                if isinstance(params, list): params = params[0]
                params = params.detach().cuda()
            params *= sd
        else: print(' Snapshot not found:', resume); exit()
    else:
        if isinstance(resume, list): resume = resume[0]
        params = resume.cuda()
    return params, size

def fft_image(shape, sd=0.01, decay_power=1.0, resume=None): # decay ~ blur

    params, size = resume_fft(resume, shape, decay_power, sd=sd)
    spectrum_real_imag_t = params.requires_grad_(True)
    if size is not None: shape[2:] = size
    [h,w] = list(shape[2:])

    freqs = rfft2d_freqs(h,w)
    scale = 1. / np.maximum(freqs, 4./max(h,w)) ** decay_power
    scale *= np.sqrt(h*w)
    scale = torch.tensor(scale).float()[None, None, ..., None].cuda()

    def inner(shift=None, contrast=1.):
        scaled_spectrum_t = scale * spectrum_real_imag_t
        if shift is not None:
            scaled_spectrum_t += scale * shift
        if float(torch.__version__[:3]) < 1.8:
            image = torch.irfft(scaled_spectrum_t, 2, normalized=True, signal_sizes=(h, w))
        else:
            if type(scaled_spectrum_t) is not torch.complex64:
                scaled_spectrum_t = torch.view_as_complex(scaled_spectrum_t)
            image = torch.fft.irfftn(scaled_spectrum_t, s=(h, w), norm='ortho')
        image = image * contrast / image.std() # keep contrast, empirical
        return image

    return [spectrum_real_imag_t], inner, size

def inv_sigmoid(x):
    eps = 1.e-12
    x = torch.clamp(x.double(), eps, 1-eps)
    y = torch.log(x/(1-x))
    return y.float()

def un_rgb(image, colors=1.):
    color_correlation_svd_sqrt = np.asarray([[0.26, 0.09, 0.02], [0.27, 0.00, -0.05], [0.27, -0.09, 0.03]]).astype("float32")
    color_correlation_svd_sqrt /= np.asarray([colors, 1., 1.])
    max_norm_svd_sqrt = np.max(np.linalg.norm(color_correlation_svd_sqrt, axis=0))
    color_correlation_normalized = color_correlation_svd_sqrt / max_norm_svd_sqrt
    color_uncorrelate = np.linalg.inv(color_correlation_normalized)

    image = inv_sigmoid(image)
    t_permute = image.permute(0,2,3,1)
    t_permute = torch.matmul(t_permute, torch.tensor(color_uncorrelate.T).cuda())
    image = t_permute.permute(0,3,1,2)
    return image

def un_spectrum(spectrum, decay_power):
    h = spectrum.shape[2]
    w = (spectrum.shape[3]-1)*2
    freqs = rfft2d_freqs(h, w)
    scale = 1.0 / np.maximum(freqs, 1.0 / max(w, h)) ** decay_power
    scale *= np.sqrt(w*h)
    scale = torch.tensor(scale).float()[None, None, ..., None].cuda()
    return spectrum / scale

def img2fft(img_in, decay=1., colors=1.):
    h, w = img_in.shape[0], img_in.shape[1]
    img_in = torch.Tensor(img_in).cuda().permute(2,0,1).unsqueeze(0) / 255.
    img_in = un_rgb(img_in, colors=colors)

    with torch.no_grad():
        if float(torch.__version__[:3]) < 1.8:
            spectrum = torch.rfft(img_in, 2, normalized=True) # 1.7
        else:
            spectrum = torch.fft.rfftn(img_in, s=(h, w), dim=[2,3], norm='ortho') # 1.8
            spectrum = torch.view_as_real(spectrum)
        spectrum = un_spectrum(spectrum, decay_power=decay)
        spectrum *= 500000. # [sic!!!]
    return spectrum


def main():
    a = get_args()

    prev_enc = 0
    def train(i):
        loss = 0
        
        noise = a.noise * torch.rand(1, 1, *params[0].shape[2:4], 1).cuda() if a.noise > 0 else None
        img_out = image_f(noise)
        img_sliced = slice_imgs([img_out], a.samples, a.modsize, trform_f, a.align, macro=a.macro)[0]
        out_enc = model_clip.encode_image(img_sliced)

        if a.in_txt is not None: # input text
            loss +=  sign * torch.cosine_similarity(txt_enc, out_enc, dim=-1).mean()
            if a.notext > 0:
                loss -= sign * a.notext * torch.cosine_similarity(txt_plot_enc, out_enc, dim=-1).mean()
        if a.in_txt2 is not None: # input text - style
            loss +=  sign * 0.5 * torch.cosine_similarity(txt_enc2, out_enc, dim=-1).mean()
        if a.in_txt0 is not None: # subtract text
            loss += -sign * torch.cosine_similarity(txt_enc0, out_enc, dim=-1).mean()
        if a.in_img is not None and os.path.isfile(a.in_img): # input image
            loss +=  sign * 0.5 * torch.cosine_similarity(img_enc, out_enc, dim=-1).mean()
        if a.sync > 0 and a.in_img is not None and os.path.isfile(a.in_img): # image composition
            prog_sync = (a.steps // a.fstep - i) / (a.steps // a.fstep)
            loss += prog_sync * a.sync * sim_loss(F.interpolate(img_out, sim_size).float(), img_in, normalize=True).squeeze()
        if a.sharp != 0 and a.dwt is not True: # scharr|sobel|default
            loss -= a.sharp * derivat(img_out, mode='sobel')
            # loss -= a.sharp * derivat(img_sliced, mode='scharr')
        if a.diverse != 0:
            img_sliced = slice_imgs([image_f(noise)], a.samples, a.modsize, trform_f, a.align, macro=a.macro)[0]
            out_enc2 = model_clip.encode_image(img_sliced)
            loss += a.diverse * torch.cosine_similarity(out_enc, out_enc2, dim=-1).mean()
            del out_enc2; torch.cuda.empty_cache()
        if a.expand > 0:
            global prev_enc
            if i > 0:
                loss += a.expand * torch.cosine_similarity(out_enc, prev_enc, dim=-1).mean()
            prev_enc = out_enc.detach()

        del img_out, img_sliced, out_enc; torch.cuda.empty_cache()
        assert not isinstance(loss, int), ' Loss not defined, check the inputs'
        
        if a.prog is True:
            lr_cur = lr0 + (i / a.steps) * (lr1 - lr0)
            for g in optimizer.param_groups: 
                g['lr'] = lr_cur
    
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if i % a.fstep == 0:
            with torch.no_grad():
                img = image_f(contrast=a.contrast).cpu().numpy()[0]
            # empirical tone mapping
            if (a.sync > 0 and a.in_img is not None):
                img = img **1.3
            elif a.sharp != 0:
                img = img ** (1 + a.sharp/2.)
            checkout(img, os.path.join(tempdir, '%04d.jpg' % (i // a.fstep)), verbose=a.verbose)
            pbar.upd()

    # Load CLIP models
    use_jit = True if float(torch.__version__[:3]) < 1.8 else False
    model_clip, _ = clip.load(a.model, jit=use_jit)
    try:
        a.modsize = model_clip.visual.input_resolution 
    except:
        a.modsize = 288 if a.model == 'RN50x4' else 384 if a.model == 'RN50x16' else 224
    if a.verbose is True: print(' using model', a.model)
    xmem = {'ViT-B/16':0.25, 'RN50':0.5, 'RN50x4':0.16, 'RN50x16':0.06, 'RN101':0.33}
    if a.model in xmem.keys():
        a.samples = int(a.samples * xmem[a.model])
            
    if a.multilang is True:
        model_lang = SentenceTransformer('clip-ViT-B-32-multilingual-v1').cuda()

    def enc_text(txt):
        if a.multilang is True:
            emb = model_lang.encode([txt], convert_to_tensor=True, show_progress_bar=False)
        else:
            emb = model_clip.encode_text(clip.tokenize(txt).cuda())
        return emb.detach().clone()
    
    if a.diverse != 0:
        a.samples = int(a.samples * 0.5)
    if a.sync > 0:
        a.samples = int(a.samples * 0.5)
            
    if a.transform is True:
        # trform_f = transforms.transforms_custom  
        trform_f = transforms.transforms_elastic
        a.samples = int(a.samples * 0.95)
    else:
        trform_f = transforms.normalize()

    out_name = []
    if a.in_txt is not None:
        if a.verbose is True: print(' topic text: ', basename(a.in_txt))
        if a.translate:
            translator = Translator()
            a.in_txt = translator.translate(a.in_txt, dest='en').text
            if a.verbose is True: print(' translated to:', a.in_txt) 
        txt_enc = enc_text(a.in_txt)
        out_name.append(txt_clean(a.in_txt))

        if a.notext > 0:
            txt_plot = torch.from_numpy(plot_text(a.in_txt, a.modsize)/255.).unsqueeze(0).permute(0,3,1,2).cuda()
            txt_plot_enc = model_clip.encode_image(txt_plot).detach().clone()

    if a.in_txt2 is not None:
        if a.verbose is True: print(' style text:', basename(a.in_txt2))
        a.samples = int(a.samples * 0.75)
        if a.translate:
            translator = Translator()
            a.in_txt2 = translator.translate(a.in_txt2, dest='en').text
            if a.verbose is True: print(' translated to:', a.in_txt2) 
        txt_enc2 = enc_text(a.in_txt2)
        out_name.append(txt_clean(a.in_txt2))

    if a.in_txt0 is not None:
        if a.verbose is True: print(' subtract text:', basename(a.in_txt0))
        a.samples = int(a.samples * 0.75)
        if a.translate:
            translator = Translator()
            a.in_txt0 = translator.translate(a.in_txt0, dest='en').text
            if a.verbose is True: print(' translated to:', a.in_txt0) 
        txt_enc0 = enc_text(a.in_txt0)
        out_name.append('off-' + txt_clean(a.in_txt0))

    if a.multilang is True: del model_lang

    if a.in_img is not None and os.path.isfile(a.in_img):
        if a.verbose is True: print(' ref image:', basename(a.in_img))
        img_in = torch.from_numpy(img_read(a.in_img)/255.).unsqueeze(0).permute(0,3,1,2).cuda()
        img_in = img_in[:,:3,:,:] # fix rgb channels
        in_sliced = slice_imgs([img_in], a.samples, a.modsize, transforms.normalize(), a.align)[0]
        img_enc = model_clip.encode_image(in_sliced).detach().clone()
        if a.sync > 0:
            sim_loss = lpips.LPIPS(net='vgg', verbose=False).cuda()
            sim_size = [s//2 for s in a.size]
            img_in = F.interpolate(img_in, sim_size).float()
        else:
            del img_in
        del in_sliced; torch.cuda.empty_cache()
        out_name.append(basename(a.in_img).replace(' ', '_'))

    shape = [1, 3, *a.size]
    if a.dwt is True:
        params, image_f, sz = dwt_image(shape, a.wave, a.sharp, a.colors, a.resume)
    else:
        params, image_f, sz = fft_image(shape, 0.01, a.decay, a.resume)
    if sz is not None: a.size = sz
    image_f = to_valid_rgb(image_f, colors = a.colors)

    if a.prog is True:
        lr1 = a.lrate * 2
        lr0 = lr1 * 0.01
    else:
        lr0 = a.lrate
    optimizer = torch.optim.AdamW(params, lr0, weight_decay=0.01, amsgrad=True)
    sign = 1. if a.invert is True else -1.

    if a.verbose is True: print(' samples:', a.samples)
    out_name = '-'.join(out_name)
    out_name += '-%s' % a.model if 'RN' in a.model.upper() else ''
    tempdir = os.path.join(a.out_dir, out_name)
    os.makedirs(tempdir, exist_ok=True)

    pbar = ProgressBar(a.steps // a.fstep)
    for i in range(a.steps):
        train(i)

    os.system('ffmpeg -v warning -y -i %s\%%04d.jpg "%s.mp4"' % (tempdir, os.path.join(a.out_dir, out_name)))
    shutil.copy(img_list(tempdir)[-1], os.path.join(a.out_dir, '%s-%d.jpg' % (out_name, a.steps)))
    if a.save_pt is True:
        torch.save(params, '%s.pt' % os.path.join(a.out_dir, out_name))

if __name__ == '__main__':
    main()
