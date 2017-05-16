import math, pafy, json, sys, os, os.path, moviepy, os
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import *
import tensorflow as tf
from tensorflow.python.platform import gfile
import random
import subprocess



def crop_and_resize_video(video_path, output_dir, video_file_clip=None, target_format=(128, 128), relative_crop_displacement=0.0):
  """
  :param video_path: specifies the full (absolute) path to the video file
  :param output_dir: specifies the directory for storing the adapted video file
  :param video_file_clip: provide a moviepy.VideoFileClip if this should be used instead of the video specified by video_path
  :param target_format: a tuple (width, height) specifying the dimensions of the returned video
  :param relative_crop_displacement: augmentation parameter, adjusts the clipping in either
  y (when width < height) or x (when width >= height) dimension
  :return: returns the cropped and resized VideoFileClip instance
  """

  assert os.path.isfile(video_path), "video_path is not a file"
  assert os.path.isdir(output_dir), "output dir is not a directory"
  assert (-1 <= relative_crop_displacement <= 1), "relative_crop_displacement must be in interval [0,1]"

  if video_file_clip is not None and hasattr(video_file_clip, 'reader'):
    clip = video_file_clip
  else:
    clip = VideoFileClip(video_path)

  width, height = clip.size
  if width >= height:
    x1 = math.floor((width - height) / 2) + relative_crop_displacement * math.floor((width - height) / 2)
    y1 = None
    size = height
  else:
    x1 = None
    y1 = math.floor((height - width) / 2) + relative_crop_displacement * math.floor((height - width) / 2)
    size = width

  clip_crop = moviepy.video.fx.all.crop(clip, x1=x1, y1=y1, width=size, height=size)
  return moviepy.video.fx.all.resize(clip_crop, newsize=target_format)

def get_ucf_video_category(taxonomy_list, label):
  """requires the ucf101 taxonomy tree structure (from json file) and the clip label (e.g. 'Surfing)
  in order to find the corresponding category (e.g. 'Participating in water sports)''"""
  return search_list_of_dicts(taxonomy_list, 'nodeName', label)[0]['parentName']


def search_list_of_dicts(list_of_dicts, dict_key, dict_value):
  """parses through a list of dictionaries in order to return an entire dict entry that matches a given value (dict_value)
  for a given key (dict_key)"""
  return [entry for entry in list_of_dicts if entry[dict_key] == dict_value]


def get_metadata_dict_as_bytes(value):
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def get_filename_filetype_from_path(path):
  """extracts and returns both filename (e.g. 'video_1') and filetype (e.g. 'mp4') from a given absolute path"""
  filename = os.path.basename(path)
  video_id, filetype = filename.split(".")
  return video_id, filetype


def get_metadata_dict_entry(clip_dict_entry, taxonomy_list):
  """constructs and returns a dict entry consisting of the following entries (with UCF 101 example values):
  - label (Scuba diving)
  - category (Participating in water sports)
  - video_id (m_ST2LDe5lA_0)
  - filetype (mp4)
  - duration (12.627)
  - mode (training)
  - url (https://www.youtube.com/watch?v=m_ST2LDe5lA)"""
  meta_dict_entry = {}

  label = clip_dict_entry['label']
  video_path = clip_dict_entry['path']
  duration = clip_dict_entry['duration']
  url = clip_dict_entry['url']
  mode = clip_dict_entry['subset']
  video_id, filetype = get_filename_filetype_from_path(video_path)
  category = get_ucf_video_category(taxonomy_list, label)

  meta_dict_entry['label'] = label
  meta_dict_entry['category'] = category
  meta_dict_entry['video_id'] = video_id
  meta_dict_entry['filetype'] = filetype
  meta_dict_entry['duration'] = duration
  meta_dict_entry['mode'] = mode
  meta_dict_entry['url'] = url

  return meta_dict_entry

def create_ucf101_metadata_dicts(subclip_dict, database_dict):
  """construcs a list of dicts. Requires the json.load returns of the metadata files;
  single clip dict and database / taxonomy dict (e.g. UCF101: metadata_subclips.json and metadata.json"""
  taxonomy_list = database_dict['taxonomy']
  meta_dict = []

  for i, (key, clip) in enumerate(subclip_dict.items()):
    entry = get_metadata_dict_entry(clip, taxonomy_list)
    meta_dict.append(entry)

  return meta_dict

def extract_subvideo(video_path, target_time_interval=(1, 4)):
  """ Returns an instance of VideoFileClip of the initial video with the content between times start_time and end_time.
  In the case end_time exceeds the true end_time of the clip the entire clip starting from start_time is returned.
  Should the video's duration be smaller than the given start_time, the original video is returned immediately without
  trimming. Also, should the specified subvideo length (end_time - start_time) exceed the video duration, the original
  video is returned.

  :param video_path: specifies the full (absolute) path to the video file
  :param target_time_interval(x,y): x: start time in s (e.g. 6.5) y: end time in s (e.g. 6.5)
  :return: the trimmed sub video (VideoFileClip)
  """

  start_time = target_time_interval[0]
  end_time = target_time_interval[1]

  assert os.path.isfile(video_path), "video_path does not contain a file"
  assert start_time < end_time, "invalid target time interval - start_time must be smaller than end_time"

  clip = VideoFileClip(video_path)

  assert end_time < clip.duration, "video to short to crop (duration=%.3f, end_time=%.3f)" % (clip.duration, end_time)

  sub_clip = clip.subclip(start_time, end_time)

  assert abs(sub_clip.duration - end_time + start_time) < 0.001 # ensure that sub_clip has desired length
  # returning both for killing since ffmpeg implementation produces zombie processes
  return sub_clip

def prepare_and_store_video(source_video_path, output_dir, target_time_interval, target_format=(128,128), relative_crop_displacement=0.0):
  sub_clip = None
  source_video_name = os.path.basename(source_video_path).replace('.mp4', '').replace('.avi', '')

  assert isinstance(target_time_interval, tuple), "provided target_time_interval is not a tuple"
  # do time trimming, else leave video at original length
  sub_clip = extract_subvideo(source_video_path, target_time_interval=(target_time_interval[0], target_time_interval[1]))
  assert sub_clip is not None

  if sub_clip is not None and sub_clip.duration < (target_time_interval[1] - target_time_interval[0]):
    # skip video if it is shorter than specified
    print('Video too short, skipping.')
    kill_process(sub_clip)
    subprocess.call(["pkill -9 -f " + source_video_path], shell=True)
    return None

  target_video_name = generate_video_name(source_video_name, target_format, relative_crop_displacement, target_time_interval)
  target_video_path = os.path.join(output_dir, target_video_name)

  clip_resized = crop_and_resize_video(source_video_path, output_dir, video_file_clip=sub_clip, target_format=target_format,
                                       relative_crop_displacement=relative_crop_displacement)

  print("writing video: " + target_video_path)
  clip_resized.write_videofile(target_video_path)
  kill_process(clip_resized)

def generate_video_name(source_video_name, target_format, relative_crop_displacement, time_interval):
  return source_video_name + '_' + str(target_format[0]) + 'x' + str(target_format[1]) + '_' \
                      + str("%.2f" % relative_crop_displacement) + '_' + "(%.1f,%.1f)" % time_interval\
                      + '.mp4'

def prepare_and_store_all_videos(subclip_json_file_location, json_file_location_taxonomy, output_dir, target_format=(128,128)):
  categories = []

  # load dictionaries
  with open(subclip_json_file_location) as file:
    subclip_dict = json.load(file)

  with open(json_file_location_taxonomy) as file:
    database_dict = json.load(file)

  # metadata = create_ucf101_metadata_dicts(subclip_dict, database_dict)

  # video_name = os.path.basename(video_path).replace('.mp4', '').replace('.avi', '')
  # dest_path = os.path.dirname(output_dir)
  file_paths = gfile.Glob(os.path.join(output_dir, '*.mp4'))
  file_names = [os.path.basename(i) for i in file_paths]

  for i, (key, clip) in enumerate(subclip_dict.items()):
    try:
      label, subset, video_path, duration = clip['label'], clip['subset'], clip['path'], clip['duration']
      video_name = os.path.basename(video_path).replace('.mp4', '').replace('.avi', '')
      if any(str(video_name) in x for x in file_names):
        print("Skipping video (already_exists): " + video_name)
        continue

      interval_suggestions = video_time_interval_suggestions(duration, max_num_suggestions=4)
      if len(interval_suggestions) == 1:
        num_random_crops = 4
      elif len(interval_suggestions) == 2:
        num_random_crops = 3
      else:
        num_random_crops = 2

      for time_interval in interval_suggestions:
        for _ in range(num_random_crops):
          sample_rel_crop_displacement = random.uniform(-0.7, 0.7)
          try:
            prepare_and_store_video(video_path, output_dir, target_time_interval=time_interval,
                                    relative_crop_displacement=sample_rel_crop_displacement, target_format=target_format)
          except Exception as e:
            print('Failed to process video (' + str(video_path) + ') ---' + str(e))
          finally:
            subprocess.call(["pkill -9 -f " + video_path], shell=True)

    except Exception as e:
      print('Failed to process video (' + str(video_path) + ') ---' + str(e))


def kill_process(process):
  if hasattr(process, 'reader'):
    process.reader.close()
  if hasattr(process, 'audio'):
    process.audio.reader.close_proc()

  try:
    process.__del__()
  except:
    pass

def video_time_interval_suggestions(video_duration, max_num_suggestions=4):
  """
  :param video_length: duration of video in seconds
  :return: array of tuples representing time intervals [(t1_start, t1_end), (t2_start, t2_end), ...]
  """
  assert (video_duration > 3), "video to short to crop (duration < 3 sec)"
  suggestions = []
  if video_duration < 4:
    margin = (4-video_duration)/2
    suggestions.append((margin, 4-margin))
  elif video_duration < 5:
    suggestions.append((1, 4))
  else:
    num_suggestuions = min(max_num_suggestions, int((video_duration-2)//2.5))
    left_margin, right_margin= 1, video_duration - 1
    for i in range(num_suggestuions):
      offset = (video_duration-2)/num_suggestuions * i
      suggestions.append((left_margin + offset, left_margin + offset + 3))

  assert len(suggestions) > 0
  assert all([t_end <= video_duration and abs(t_end - t_start - 3) < 0.001 for t_start, t_end in suggestions])

  return suggestions

def main():
  # load subclip dict
  json_file_location = '/common/homes/students/rothfuss/Downloads/clips/metadata_subclips.json'
  json_file_location_taxonomy = '/common/homes/students/rothfuss/Downloads/metadata.json'
  output_dir = '/data/rothfuss/data/ucf101_prepared_videos/'

  prepare_and_store_all_videos(json_file_location, json_file_location_taxonomy, output_dir)


if __name__ == '__main__':
 main()