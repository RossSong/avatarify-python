from scipy.spatial import ConvexHull
import torch
import yaml
from modules.keypoint_detector import KPDetector
from modules.generator_optim import OcclusionAwareGenerator
from sync_batchnorm import DataParallelWithCallback
import numpy as np
import face_alignment
from animate import normalize_kp


def to_tensor(a):
    return torch.tensor(a[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2) / 255


class PredictorLocal:
    def __init__(self, config_path, checkpoint_path, relative=False, adapt_movement_scale=False, device=None, enc_downscale=1):
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.relative = relative
        self.adapt_movement_scale = adapt_movement_scale
        self.start_frame = None
        self.start_frame_kp = None
        self.kp_driving = None
        self.kp_driving_initial = None
        self.config_path = config_path
        self.checkpoint_path = checkpoint_path
        self.generator, self.kp_detector = self.load_checkpoints()
        self.fa = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D, flip_input=True, device=self.device)
        self.source = None
        self.kp_source = None
        self.enc_downscale = enc_downscale
        self.face_deviation = 0, 0

    def load_checkpoints(self):
        with open(self.config_path) as f:
            config = yaml.load(f)
    
        generator = OcclusionAwareGenerator(**config['model_params']['generator_params'],
                                            **config['model_params']['common_params'])
        generator.to(self.device)
    
        kp_detector = KPDetector(**config['model_params']['kp_detector_params'],
                                 **config['model_params']['common_params'])
        kp_detector.to(self.device)
    
        checkpoint = torch.load(self.checkpoint_path, map_location=self.device)
        generator.load_state_dict(checkpoint['generator'])
        kp_detector.load_state_dict(checkpoint['kp_detector'])
    
        generator.eval()
        kp_detector.eval()
        
        return generator, kp_detector

    def reset_frames(self):
        self.kp_driving_initial = None

    def set_deform_mask(self, mask):
        mask = torch.tensor(mask).to(self.device).float() / 255
        mask = mask[None, ..., None]
        self.generator.set_deform_mask(mask)

    def set_source_image(self, source_image):
        self.source = to_tensor(source_image).to(self.device)
        self.kp_source = self.kp_detector(self.source)

        if self.enc_downscale > 1:
            h, w = int(self.source.shape[2] / self.enc_downscale), int(self.source.shape[3] / self.enc_downscale)
            source_enc = torch.nn.functional.interpolate(self.source, size=(h, w), mode='bilinear')
        else:
            source_enc = self.source

        self.generator.encode_source(source_enc)

    def update_kp_driving(self, kp_driving):
        if self.kp_driving is None:
            self.kp_driving = kp_driving

        center_initial = self.kp_driving_initial['value'][0].mean(0)
        center_driving = kp_driving['value'][0].mean(0)
        #d_x, d_y = (center_initial - center_driving).cpu().numpy()
        d_x, d_y = center_driving.cpu().numpy()

        d_th_x = 0.25
        d_th_y = 0.15

        if d_x > 0:
            dev_x = max(0, d_x - d_th_x)
        else:
            dev_x = min(0, d_x + d_th_x)
        
        if d_y > 0:
            dev_y = max(0, d_y - d_th_y)
        else:
            dev_y = min(0, d_y + d_th_y)

        if dev_x == 0 and dev_y == 0:
            self.kp_driving = kp_driving
            kp_out = kp_driving
        else:
            kp_zero_mean = kp_driving['value'] - kp_driving['value'].mean(1, keepdim=True)
            kp_out = {
                # 'value': self.kp_driving['value'] + kp_zero_mean,
                'value': self.kp_driving['value'].mean(1, keepdim=True) + kp_zero_mean,
                'jacobian': kp_driving['jacobian']
            }

        # self.face_deviation = dev_x, dev_y
        self.face_deviation = d_x, d_y

        print((d_x, d_y), (dev_x, dev_y), center_driving)

        # self.kp_driving['value'] = kp_driving['value'] * alpha + self.kp_driving['value'] * (1 - alpha)
        # self.kp_driving['jacobian'] = kp_driving['jacobian']

        return kp_out

    def get_face_deviation(self):
        return self.face_deviation
        
    def predict(self, driving_frame):
        assert self.kp_source is not None, "call set_source_image()"
        
        with torch.no_grad():
            driving = to_tensor(driving_frame).to(self.device)

            if self.kp_driving_initial is None:
                self.kp_driving_initial = self.kp_detector(driving)
                self.start_frame = driving_frame.copy()
                self.start_frame_kp = self.get_frame_kp(driving_frame)

            kp_driving = self.kp_detector(driving)
            kp_driving_upd = self.update_kp_driving(kp_driving)

            kp_norm = normalize_kp(kp_source=self.kp_source, kp_driving=kp_driving_upd,
                                kp_driving_initial=self.kp_driving_initial, use_relative_movement=self.relative,
                                use_relative_jacobian=self.relative, adapt_movement_scale=self.adapt_movement_scale)

            out = self.generator(self.source, kp_source=self.kp_source, kp_driving=kp_norm)

            out = np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0]
            out = (np.clip(out, 0, 1) * 255).astype(np.uint8)

            return out

    def get_frame_kp(self, image):
        kp_landmarks = self.fa.get_landmarks(image)
        if kp_landmarks:
            kp_image = kp_landmarks[0]
            kp_image = self.normalize_alignment_kp(kp_image)
            return kp_image
        else:
            return None

    @staticmethod
    def normalize_alignment_kp(kp):
        kp = kp - kp.mean(axis=0, keepdims=True)
        area = ConvexHull(kp[:, :2]).volume
        area = np.sqrt(area)
        kp[:, :2] = kp[:, :2] / area
        return kp
    
    def get_start_frame(self):
        return self.start_frame

    def get_start_frame_kp(self):
        return self.start_frame_kp
