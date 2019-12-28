# Copyright (C) 2019 Jin Han Lee
#
# This file is a part of BTS.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>

import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
import torch.utils.data.distributed
from torchvision import transforms
from PIL import Image
import os
import random


def _is_pil_image(img):
    return isinstance(img, Image.Image)


def _is_numpy_image(img):
    return isinstance(img, np.ndarray) and (img.ndim in {2, 3})

def preprocessing_transforms(is_test):
    return transforms.Compose([
        ToTensor(is_test=is_test)
    ])


class BtsDataLoader(object):
    def __init__(self, args):
        if args.mode == 'train':
            self.training_samples = DataLoadPreprocess(args, transform=preprocessing_transforms(False))
            if args.distributed:
                self.train_sampler = torch.utils.data.distributed.DistributedSampler(self.training_samples)
            else:
                self.train_sampler = None
    
            self.data = DataLoader(self.training_samples, args.batch_size,
                                   shuffle=(self.train_sampler is None),
                                   num_workers=args.num_threads,
                                   pin_memory=True,
                                   sampler=self.train_sampler)
        
        else:
            self.testing_samples = DataLoadPreprocess(args, transform=preprocessing_transforms(True))
            self.data = DataLoader(self.testing_samples, 1, shuffle=False, num_workers=1)
            
            
class DataLoadPreprocess(Dataset):
    def __init__(self, args, transform=None):
        self.args = args
        with open(args.filenames_file, 'r') as f:
            self.filenames = f.readlines()
    
        self.mode = args.mode
        self.transform = transform
        self.to_tensor = ToTensor
    
    def __getitem__(self, idx):
        sample_path = self.filenames[idx]
        focal = float(sample_path.split()[-1])

        if self.mode == 'train':
            image_path = os.path.join(self.args.data_path, "./" + sample_path.split()[0])
            depth_path = os.path.join(self.args.gt_path, "./" + sample_path.split()[1])
    
            image = Image.open(image_path)
            depth_gt = Image.open(depth_path)
            
            if self.args.do_kb_crop is True:
                height = image.height
                width = image.width
                top_margin = int(height - 352)
                left_margin = int((width - 1216) / 2)
                depth_gt = depth_gt.crop((left_margin, top_margin, left_margin + 1216, top_margin + 352))
                image = image.crop((left_margin, top_margin, left_margin + 1216, top_margin + 352))
            
            # To avoid blank boundaries due to pixel registration
            if self.args.dataset == 'nyu':
                depth_gt = depth_gt.crop((43, 45, 608, 472))
                image = image.crop((43, 45, 608, 472))
    
            if self.args.do_random_rotate is True:
                random_angle = (random.random() - 0.5) * 2 * self.args.degree
                image = self.rotateImage(image, random_angle)
                depth_gt = self.rotateImage(depth_gt, random_angle, flag=Image.NEAREST)
            
            image = np.asarray(image, dtype=np.float32) / 255.0
            depth_gt = np.asarray(depth_gt, dtype=np.float32)
            depth_gt = np.expand_dims(depth_gt, axis=2)

            if self.args.dataset == 'nyu':
                depth_gt = depth_gt / 1000.0
            else:
                depth_gt = depth_gt / 256.0

            image, depth_gt = self.random_crop(image, depth_gt, self.args.input_height, self.args.input_width)
            image, depth_gt = self.train_preprocess(image, depth_gt)
            sample = {'image': image, 'depth': depth_gt, 'focal': focal}
        
        else:
            image_path = os.path.join(self.args.data_path, "./" + sample_path.split()[0])
            image = np.asarray(Image.open(image_path), dtype=np.float32) / 255.0

            if self.args.do_kb_crop is True:
                height = image.shape[0]
                width = image.shape[1]
                top_margin = int(height - 352)
                left_margin = int((width - 1216) / 2)
                image = image[top_margin:top_margin + 352, left_margin:left_margin + 1216, :]
            
            sample = {'image': image, 'focal': focal}
        
        if self.transform:
            sample = self.transform(sample)
        
        return sample
    
    def rotateImage(self, image, angle, flag=Image.BILINEAR):
        result = image.rotate(angle, resample=flag)
        return result

    def random_crop(self, img, depth, height, width):
        assert img.shape[0] >= height
        assert img.shape[1] >= width
        assert img.shape[0] == depth.shape[0]
        assert img.shape[1] == depth.shape[1]
        x = random.randint(0, img.shape[1] - width)
        y = random.randint(0, img.shape[0] - height)
        img = img[y:y + height, x:x + width, :]
        depth = depth[y:y + height, x:x + width, :]
        return img, depth

    def train_preprocess(self, image, depth_gt):
        # Random flipping
        do_flip = random.random()
        if do_flip > 0.5:
            image = (image[:, ::-1, :]).copy()
            depth_gt = (depth_gt[:, ::-1, :]).copy()
    
        # Random gamma, brightness, color augmentation
        do_augment = random.random()
        if do_augment > 0.5:
            image = self.augment_image(image)
    
        return image, depth_gt
    
    def augment_image(self, image):
        # gamma augmentation
        gamma = random.uniform(0.9, 1.1)
        image_aug = image ** gamma

        # brightness augmentation
        if self.args.dataset == 'nyu':
            brightness = random.uniform(0.75, 1.25)
        else:
            brightness = random.uniform(0.9, 1.1)
        image_aug = image_aug * brightness

        # color augmentation
        colors = np.random.uniform(0.9, 1.1, size=3)
        white = np.ones((image.shape[0], image.shape[1]))
        color_image = np.stack([white * colors[i] for i in range(3)], axis=2)
        image_aug *= color_image
        image_aug = np.clip(image_aug, 0, 1)

        return image_aug
    
    def __len__(self):
        return len(self.filenames)


class ToTensor(object):
    def __init__(self, is_test=False):
        self.is_test = is_test
        self.normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    
    def __call__(self, sample):
        image, focal = sample['image'], sample['focal']
        image = self.to_tensor(image)
        image = self.normalize(image)
        
        if not self.is_test:
            depth = sample['depth']
            depth = self.to_tensor(depth)
            return {'image': image, 'depth': depth, 'focal': focal}
        
        return {'image': image, 'focal': focal}
    
    def to_tensor(self, pic):
        if not (_is_pil_image(pic) or _is_numpy_image(pic)):
            raise TypeError(
                'pic should be PIL Image or ndarray. Got {}'.format(type(pic)))
        
        if isinstance(pic, np.ndarray):
            img = torch.from_numpy(pic.transpose((2, 0, 1)))
            return img
        
        # handle PIL Image
        if pic.mode == 'I':
            img = torch.from_numpy(np.array(pic, np.int32, copy=False))
        elif pic.mode == 'I;16':
            img = torch.from_numpy(np.array(pic, np.int16, copy=False))
        else:
            img = torch.ByteTensor(torch.ByteStorage.from_buffer(pic.tobytes()))
        # PIL image mode: 1, L, P, I, F, RGB, YCbCr, RGBA, CMYK
        if pic.mode == 'YCbCr':
            nchannel = 3
        elif pic.mode == 'I;16':
            nchannel = 1
        else:
            nchannel = len(pic.mode)
        img = img.view(pic.size[1], pic.size[0], nchannel)
        
        img = img.transpose(0, 1).transpose(0, 2).contiguous()
        if isinstance(img, torch.ByteTensor):
            return img.float()
        else:
            return img
        