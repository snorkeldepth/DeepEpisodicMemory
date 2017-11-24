import numpy as np
import cv2 as cv2



def compute_dense_optical_flow(prev_image, current_image, pyr_scale=0.8, levels=15, winsize=5, iterations=10,
                               poly_n=5, poly_sigma=1.5, flags=0):
  """
  Computes the Farneback optical flow between prev_iamge and current_image. For parameter specification, refer to
   'docs.opencv.org/2.4/modules/video/doc/motion_analysis_and_object_tracking.html#calcopticalflowfarneback'
  :param prev_image: a color (3-channel) image of shape (HEIGHT_VIDEO, WIDTH_VIDEO, [B, G, R]) as uint8
  :param current_image: a color (3-channel) image of shape (HEIGHT_VIDEO, WIDTH_VIDEO, [B, G, R]) as uint8
  :param pyr_scale: parameter, specifying the image scale (<1) to build pyramids for each image
  :param levels: number of pyramid layers including the initial image
  :param winsize: averaging window size
  :param iterations: number of iterations the algorithm does at each pyramid level
  :param poly_n: size of the pixel neighborhood used to find polynomial expansion in each pixel
  :param poly_sigma: standard deviation of the Gaussian that is used to smooth derivatives used as a basis for the polynomial expansion
  :param flags: operation flags, see above specification
  :return: a 3-channel 'flow' frame containing the Farneback flow between the provided two images equaling the
  shape of the input images (also BGR)
  """
  old_shape = current_image.shape
  prev_image_gray = cv2.cvtColor(prev_image, cv2.COLOR_BGR2GRAY)
  current_image_gray = cv2.cvtColor(current_image, cv2.COLOR_BGR2GRAY)
  assert current_image.shape == old_shape
  hsv = np.zeros_like(prev_image)
  hsv[..., 1] = 255

  flow = cv2.calcOpticalFlowFarneback(prev_image_gray, current_image_gray, pyr_scale, levels, winsize, iterations, poly_n, poly_sigma, flags)

  mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
  hsv[..., 0] = ang*180/np.pi/2
  hsv[..., 2] = cv2.normalize(mag, None, 0, 255, cv2.NORM_MINMAX)
  return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def getVideoCapture(path):
    cap = None
    if path:
      cap = cv2.VideoCapture(path)
    return cap


def getNextFrame(cap):
  ret, frame = cap.read()
  if ret == False:
    return None

  return np.asarray(frame)