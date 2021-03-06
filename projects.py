import csv
import glob
import json
import os
import random
import re
import shutil
import subprocess
import logging
from collections import namedtuple
import flask
import numpy as np
from werkzeug.utils import secure_filename
from drone.drone_log_data import drone_log
from forms import NewProjectForm, EditProjectForm
from drone.fov import fov
from help_functions import get_last_modified_time, get_last_updated_time_as_string, base_dir, celery, tasks

logger = logging.getLogger('app.' + __name__)

project_tuple = namedtuple('project', ['title', 'description', 'last_updated'])
projects_view = flask.Blueprint('projects', __name__)


@projects_view.route('/', methods=['GET', 'POST'])
def index():
    random_int = random.randint(1, 10000000)
    project_dict = make_project_dict()
    res, new_project_form = get_new_project_form(project_dict)
    if res:
        return res
    edit_project_form = get_edit_project_form(project_dict)
    arguments = {'projects': project_dict, 'random_int': random_int, 'new_project_form': new_project_form, 'edit_project_form': edit_project_form}
    logger.debug(f'Render index')
    return flask.render_template('projects/index.html', **arguments)


def make_project_dict():
    logger.debug(f'Finding all projects')
    project_dict = {}
    project_dir = os.path.join(base_dir, 'projects')
    if not os.path.exists(project_dir):
        os.mkdir(project_dir)
    projects = next(os.walk(project_dir))[1]
    for project_title in projects:
        last_modified_string = get_last_modified_time('projects', project_title)
        with open(os.path.join(base_dir, 'projects', project_title, 'description.txt'), 'r') as file:
            description = flask.Markup(file.read())
        project = project_tuple(project_title, description, last_modified_string)
        project_dict.update({project_title: project})
    return project_dict


def get_new_project_form(project_dict):
    form = NewProjectForm()
    drone_dir = os.path.join(base_dir, 'drones')
    if not os.path.exists(drone_dir):
        os.mkdir(drone_dir)
    drones = next(os.walk(drone_dir))[2]
    drones = [x for x in drones if x.endswith('.txt')]
    form.drone.choices = [(d, d[:-4]) for d in drones]
    if form.validate_on_submit():
        project_title = form.name.data
        logger.debug(f'Creating a new project with name {project_title}')
        description = form.description.data
        drone = form.drone.data
        if project_title in project_dict:
            flask.flash('A project with that name already exist!')
        else:
            os.mkdir(os.path.join(base_dir, 'projects', project_title))
            with open(os.path.join(base_dir, 'projects', project_title, 'description.txt'), 'w') as file:
                file.write(description)
            shutil.copyfile(os.path.join(base_dir, 'drones', drone[:-4] + '.cam.npz'), os.path.join(base_dir, 'projects', project_title, 'drone.cam.npz'))
            log_file = os.path.join(base_dir, 'projects', project_title, 'drone_log.csv')
            form.log_file.data.save(log_file)
            return flask.redirect(flask.url_for('projects.upload', project=project_title)), form
    return None, form


def get_edit_project_form(project_dict):
    form = EditProjectForm()
    drones = next(os.walk(os.path.join(base_dir, 'drones')))[2]
    drones = [x for x in drones if x.endswith('.txt')]
    form.edit_drone.choices = [(d, d[:-4]) for d in drones]
    if form.validate_on_submit():
        project_before = form.edit_project_before.data
        description = form.edit_description.data
        project_title = form.edit_name.data
        drone = form.edit_drone.data
        logger.debug(f'Editing project {project_before} with new name {project_title}')
        if project_title in project_dict and not project_title == project_before:
            flask.flash('A project with that name already exist!')
        else:
            project_dict.pop(project_before, None)
            shutil.copyfile(os.path.join(base_dir, 'drones', drone[:-4] + '.cam.npz'), os.path.join(base_dir, 'projects', project_title, 'drone.cam.npz'))
            os.rename(os.path.join(base_dir, 'projects', project_before), os.path.join(base_dir, 'projects', project_title))
            with open(os.path.join(base_dir, 'projects', project_title, 'description.txt'), 'w') as file:
                file.write(description)
            last_updated = get_last_updated_time_as_string(0)
            project = project_tuple(project_title, description, last_updated)
            project_dict.update({project_title: project})
            if form.edit_log_file.data:
                video_info_files = glob.glob(os.path.join(base_dir, 'projects', project_title, '*.txt'))
                for video_info_file in video_info_files:
                    if video_info_file.rsplit(os.sep)[-1] != 'description.txt':
                        os.remove(video_info_file)
                log_file = os.path.join(base_dir, 'projects', project_title, 'drone_log.csv')
                form.edit_log_file.data.save(log_file)
    return form


@projects_view.route('/<project>/upload', methods=['GET', 'POST'])
def upload(project):
    if flask.request.method == 'POST':
        for key, file in flask.request.files.items():
            if key.startswith('file'):
                video_file = secure_filename(file.filename.rsplit('.', 1)[0] + '.mp4')
                file_location = os.path.join(base_dir, 'projects', project, video_file)
                video_mime_type = re.compile('video/*')
                if video_mime_type.match(file.mimetype):
                    while True:
                        random_int = random.randint(1, 10000000)
                        file_type = '.' + file.filename.rsplit('.', 1)[1]
                        temp_file = os.path.join(base_dir, 'projects', project, secure_filename(str(random_int) + file_type))
                        if not os.path.exists(temp_file):
                            break
                    logger.debug(f'Uploading file: {file_location}')
                    file.save(temp_file)
                    logger.debug(f'Upload done for file: {file_location}')
                    logger.debug(f'Calling celery to convert file: {temp_file} and save as {file_location}')
                    task = convert_after_upload_task.apply_async(args=(temp_file, file_location, project, video_file))
                    task_dict = {'function': convert_after_upload_task, 'video': video_file}
                    tasks.update({task.id: task_dict})
                    return flask.jsonify({}), 202
    logger.debug(f'Render upload after a GET request')
    return flask.render_template('projects/upload.html', project=project)


@celery.task(bind=True)
def convert_after_upload_task(self, temp_file, file_location, project, video_file):
    self.update_state(state='PROCESSING')
    cmd = ['ffmpeg', '-i', r'{}'.format(temp_file), '-preset', 'ultrafast', '-tune', 'zerolatency', '-an', '-loglevel', '24', '-movflags', '+faststart', '-y', r'{}'.format(file_location)]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(temp_file)
    drone_log.save_video_data_to_file(project, video_file)
    cmd = ['ffmpeg', '-i', r'{}'.format(file_location), '-vframes', '1', '-an', '-s', '300x200', '-ss', '0', r'{}.jpg'.format(file_location)]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@celery.task(bind=True)
def concat_videos_task(self, concat_file, output_file, project, video_file, first_video_file):
    self.update_state(state='PROCESSING')
    cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', r'{}'.format(concat_file), '-c', 'copy', r'{}'.format(output_file)]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    drone_log.save_video_data_to_file(project, video_file)
    cmd = ['ffmpeg', '-i', r'{}'.format(output_file), '-vframes', '1', '-an', '-s', '300x200', '-ss', '0', r'{}.jpg'.format(output_file)]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@projects_view.route('/status/<task_id>')
def task_status(task_id):
    task = tasks[task_id]['function'].AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {'state': task.state, 'status': 'Pending'}
    elif task.state == 'SUCCESS':
        response = {'state': task.state, 'status': 'Done'}
        tasks.pop(task_id, None)
    elif task.state != 'FAILURE':
        response = {'state': task.state, 'status': 'Processing'}
    else:
        response = {'state': task.state, 'status': str(task.info)}
    return flask.jsonify(response)


@projects_view.route('/<project>/video_gallery')
def video_gallery(project):
    videos_temp = [x.split(os.sep)[-1] for x in glob.glob(os.path.join(base_dir, 'projects', project, '*.mp4'))]
    video_data = [x.split(os.sep)[-1] for x in glob.glob(os.path.join(base_dir, 'projects', project, '*.mp4_data.txt'))]
    videos = sorted([x for x in videos_temp if x + '_data.txt' in video_data])
    random_int = random.randint(1, 10000000)
    success = drone_log.test_log(project)
    if success:
        logger.debug(f'Render video_gallery for {project}')
        return flask.render_template('projects/video_gallery.html', project=project, videos=videos, tasks=tasks, random_int=random_int)
    else:
        message = 'Error interpreting the drone log file. Try and upload the log file again.'
        flask.flash(message, 'error')
        logger.debug(f'Render drone log error page for {project}')
        return flask.render_template('projects/error.html')


@projects_view.route('/<project>/concatenate_videos')
def concat_videos(project):
    videos = sorted([x.split(os.sep)[-1] for x in glob.glob(os.path.join(base_dir, 'projects', project, '*.mp4'))])
    logger.debug(f'Render concat_videos for {project}')
    return flask.render_template('projects/concat_videos.html', project=project, videos=videos)


@projects_view.route('/<project>/concatenating', methods=['POST'])
def do_concat_videos(project):
    videos_json = flask.request.form.get('videos')
    output_file_name = flask.request.form.get('output_name')
    if output_file_name[-4:] != '.mp4':
        output_file_name += '.mp4'
    videos = json.loads(videos_json)
    video_str = ''
    for video in videos:
        video_str += 'file ' + video + '\n'
    concat_file = os.path.join(base_dir, 'projects', project, 'concat.txt')
    with open(concat_file, 'w') as file:
        file.write(video_str)
    output_file = os.path.join(base_dir, 'projects', project, output_file_name)
    logger.debug(f'Calling celery task for concat')
    task = concat_videos_task.apply_async(args=(concat_file, output_file, project, output_file_name, videos[0]))
    task_dict = {'function': concat_videos_task, 'video': output_file_name}
    tasks.update({task.id: task_dict})
    return flask.jsonify({}), 202


@projects_view.route('/<project>/download')
def download(project):
    with open('version.txt') as version_file:
        pro_version = version_file.read()
    filename = os.path.join(base_dir, 'projects', project, 'annotations.csv')
    annotations = get_all_annotations(project, pro_version)
    save_annotations_csv(annotations, filename)
    logger.debug(f'Sending annotations.csv to user.')
    return flask.send_file(filename, as_attachment=True, attachment_filename='annotations.csv')


def save_annotations_csv(annotations, filename):
    logger.debug(f'Saving annotations in {filename}')
    with open(filename, 'w') as fp:
        csv_writer = csv.writer(fp, delimiter=',')
        header = ('name', 'time', 'length', 'lat', 'lon', 'east', 'north', 'zone number', 'zone letter',
                  'image_x', 'image_y', 'video', 'project', 'pro. version')
        csv_writer.writerow(header)
        for annotation in annotations:
            if annotation:
                csv_writer.writerow(annotation)


def get_all_annotations(project, pro_version):
    logger.debug(f'Getting all annotations')
    annotations = []
    mat_file = os.path.join(base_dir, 'projects', project, 'drone.cam.npz')
    fov.set_camera_params(mat_file)
    reg_files = os.path.join(base_dir, 'projects', project, '*.json')
    files = glob.glob(reg_files)
    drone_log.get_log_data(project)
    for file in files:
        with open(file, 'r') as fp:
            json_data = json.load(fp)
        objects = json_data['objects']
        video_file = file.split(os.sep)[-1][:-5]
        drone_log.get_video_data_from_data_file(project, video_file)
        fov.set_image_size(*drone_log.video_size)
        drone_log.match_log_and_video()
        if objects:
            for obj in objects:
                if obj['type'] == 'FrameLine' or obj['type'] == 'FramePoint':
                    annotation = get_frame_obj_data(obj)
                    annotation.extend([video_file, project, pro_version])
                else:
                    logger.debug(f'Unknown annotation found of type: {obj["type"]}')
                    annotation = None
                annotations.append(annotation)
    return annotations


def get_frame_obj_data(obj):
    name = obj.get('name')
    log_data = drone_log.get_log_data_from_frame(obj.get('frame'))
    if obj['type'] == 'FrameLine':
        direction = (obj.get('x1') < 0 and obj.get('y1') < 0) or (obj.get('x2') < 0 and obj.get('y2') < 0)
        if direction:
            image_point1 = (obj.get('left'), obj.get('top'))
            image_point2 = (obj.get('left') + obj.get('width'), obj.get('top') + obj.get('height'))
        else:
            image_point1 = (obj.get('left') + obj.get('width'), obj.get('top'))
            image_point2 = (obj.get('left'), obj.get('top') + obj.get('height'))
        image_point = ((image_point1[0] + image_point2[0]) / 2, (image_point1[1] + image_point2[1]) / 2)
        wp1, zone = fov.get_world_point(image_point1, *log_data[1:], return_zone=True)
        wp2 = fov.get_world_point(image_point2, *log_data[1:])
        wp = ((wp1[0] + wp2[0]) / 2, (wp1[1] + wp2[1]) / 2)
        length = np.sqrt((wp1[0] - wp2[0]) ** 2 + (wp1[1] - wp2[1]) ** 2)
    elif obj['type'] == 'FramePoint':
        image_point = (obj.get('left'), obj.get('top'))
        wp, zone = fov.get_world_point(image_point, *log_data[1:], return_zone=True)
        length = 0
    else:
        return None
    pos = fov.convert_utm(wp[0], wp[1], zone)
    annotation = [name, log_data[0], length, pos[0], pos[1], wp[0], wp[1], zone[0], zone[1], image_point[0], image_point[1]]
    return annotation


@projects_view.route('/<project>/remove')
def remove_project(project):
    logger.debug(f'Removing project {project}')
    shutil.rmtree(os.path.join(base_dir, 'projects', project))
    return flask.redirect(flask.url_for('projects.index'))
