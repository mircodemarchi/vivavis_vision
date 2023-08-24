#!/usr/bin/env python3

# ROS libraries
import rospy
from sensor_msgs.msg import PointCloud2, Image
from visualization_msgs.msg import Marker, MarkerArray
from nav_msgs.msg import OccupancyGrid
import tf
from std_msgs.msg import String, Time, Bool
from rosgraph_msgs.msg import Clock
from geometry_msgs.msg import Point

from geometry_msgs.msg import PoseArray

# Python libraries
import numpy as np
import open3d as o3d
import math
import pandas as pd
import os
import cv2
from cv_bridge import CvBridge
import glob
import uuid
import matplotlib.pyplot as plt
from scipy.spatial import ConvexHull

from visualization_msgs.msg import Marker

from vivavis_vision.msg import WallInfo, WallInfoArray

bridge = CvBridge()

class ROS2JsonData:
    def __init__(self):
        super().__init__()

        rospy.init_node('ros_to_json_data')   
        
        self.act_cam_position = []     # store the ACTUAL camera position vector[x,y,z]
        self.walls_ordered_eq_params = [[],[],[],[],[],[]]   # ORDERED equations' parameters of "walls" found inside the scene
                       
        self.json_walls_equations_pub = rospy.Publisher('out/json_walls_equations', String, queue_size=100)
        self.json_human_workspace_pub = rospy.Publisher('out/json_human_workspace', String, queue_size=100)

        rospy.Subscriber('vivavis_vision/walls_info', WallInfoArray, self.wall_info_callback)
        rospy.Subscriber('vivavis_vision/obstacles_pose', PoseArray, self.obstacles_pose_array_callback)
        rospy.Subscriber('vivavis_vision/human_ws', Marker, self.human_ws_callback)

        
        self.listener = tf.TransformListener()
        self.camera_frame = 'camera_color_optical_frame'
        self.fixed_frame = 'world'
        # self.wall_names = ['floor','left','right','front','back','ceiling']
        # 0 = left; 1 = right; 2 = ceiling; 3 = floor; 4 = front; 5 = back
        self.wall_names = {'left':0, 'right':1,'ceiling':2,'floor':3,'front':4,'back':5}


        self.list_of_ids = [str(uuid.uuid4()) for k in range(1000)]
        
        self.act_cam_position = []     # store the ACTUAL camera position vector[x,y,z]
        self.act_cam_orientation = []  # store the ACTUAL camera orientation quaternion [x,y,z,w]
     
        self.walls_info = WallInfoArray()
        self.human_ws = Marker()
        self.obstacles = dict()
        self.walls = dict()
        self.clust_id = 0
        self.prev_obj_clust_id = 0

    def wall_info_callback(self, data):
        self.walls_info = data

    def obstacles_pose_array_callback(self, msg):
        for i, pose in enumerate(msg.poses):
            # rospy.loginfo("Obstacle: Pose %d:\nPosition: %f, %f, %f\nOrientation: %f, %f, %f, %f",
                        #   i,
                        #   pose.position.x, pose.position.y, pose.position.z,
                        #   pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)

            # self.obstacles['id' +str(i)] = [pose.position.x, pose.position.y, pose.position.z]
            self.obstacles[i] = [pose.position.x, pose.position.y, pose.position.z]

    def human_ws_callback(self, msg):
        self.human_ws = msg

    def update(self):
        self.getCameraPose()
        self.publish_json_df()
        self.publish_human_workspace()

    def getCameraPose(self):
        try:
            # Listen for the transform from camera_frame to fixed_frame
            (trans, rot) = self.listener.lookupTransform(self.fixed_frame, self.camera_frame, rospy.Time(0))
            self.act_cam_position = trans
            self.act_cam_orientation = rot
            # rospy.loginfo("Translation: %s" % str(trans))
            # rospy.loginfo("Rotation: %s" % str(rot))
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            rospy.logwarn("Transform lookup failed!")
   
    def find_absolute_closest_coordinates(self, act_x, act_y, act_z):
        (closest_obstacle, closest_wall) = self.find_closest_coordinates(act_x, act_y, act_z)
        if closest_obstacle is not None:
            closest_object_list = [closest_obstacle, closest_wall]
            # print(closest_object_list)
            min_distance = float('inf')
            for close_object in closest_object_list:
                # print(close_object)
                # print(close_object[1][0])
                # print(close_object[1][1])
                # print(close_object[1][2])
                distance = math.sqrt((close_object[1][0] - act_x)**2 + (close_object[1][1] - act_y)**2 + (close_object[1][2] - act_z)**2)
                if distance < min_distance:
                    min_distance = distance
                    closest_object = close_object
                    type_obj = str(close_object[0])
        else:
            type_obj = ''
            closest_object = closest_wall
        
        return type_obj, closest_object
        
    def find_closest_coordinates(self, act_x, act_y, act_z):
        closest_obstacle = None
        closest_wall = None
        min_obstacle_distance = float('inf')
        min_wall_distance = float('inf')

        for id, obstacle_coords in self.obstacles.items():
            distance_obs = math.sqrt((obstacle_coords[0] - act_x)**2 + (obstacle_coords[1] - act_y)**2 + (obstacle_coords[2] - act_z)**2)
            if distance_obs < min_obstacle_distance:
                min_obstacle_distance = distance_obs
                closest_obstacle = [id, obstacle_coords]

        for wall_name, wall_coords in self.walls.items():
            distance_w = math.sqrt((wall_coords[0] - act_x)**2 + (wall_coords[1] - act_y)**2 + (wall_coords[2] - act_z)**2)
            if distance_w < min_wall_distance:
                min_wall_distance = distance_w
                closest_wall = [wall_name,wall_coords]

        return closest_obstacle, closest_wall

    # Function to find distance from point to plane
    def shortest_point_plane_distance(self, x1, y1, z1, a, b, c, d):        
        d = abs((a * x1 + b * y1 + c * z1 + d))
        e = (math.sqrt(a * a + b * b + c * c))
        # print (e)
        dist = d/e
        # print("Perpendicular distance is", dist)
        return dist

    # 0 = left; 1 = right; 2 = ceiling; 3 = floor; 4 = front; 5 = back
    # Create pandas dataframe
    def publish_json_df(self):
        json_data = []
        # wall_names = ['left','right', 'ceiling', 'floor', 'front', 'back']

        # my order : floor left right front back ceiling
        for w in self.walls_info.walls:
            # print(w)
            if w.header.frame_id:
                if len(self.act_cam_position) > 0:
                    cam_x,cam_y,cam_z = self.act_cam_position
                    shortest_dist = self.shortest_point_plane_distance(self.act_cam_position[0], self.act_cam_position[1], self.act_cam_position[2], w.a, w.b, w.c, w.d)
                else:
                    shortest_dist = None

                newlist = [w.header.frame_id, w.a,w.b,w.c,w.d, shortest_dist, 
                        w.num_points, w.pose.position.x, w.pose.position.y, w.pose.position.z, w.color_id]
                json_data.append(newlist)

        if len(json_data) > 0:        
            df_json = pd.DataFrame(json_data, columns=["wall_type", "a", "b", "c", "d", "shortest_distance", "num_points", "plane_center_x", "plane_center_y", "plane_center_z", "color_id"])
            self.json_walls_equations_pub.publish(str(df_json.to_json(orient='index')))

    def publish_human_workspace(self):
        json_data = []
        for w in self.walls_info.walls:
            # print(w)
            if w.header.frame_id:
                # print(w.header.frame_id)
                self.walls[w.header.frame_id] = [w.pose.position.x, w.pose.position.y, w.pose.position.z]
        # print(self.walls)

        if len(self.act_cam_position) > 0:
            type_obj, closests_3d = self.find_absolute_closest_coordinates(self.act_cam_position[0],self.act_cam_position[1],self.act_cam_position[2])
            if closests_3d is not None:
                if (self.wall_names.get(type_obj)):
                    self.clust_id = self.wall_names[type_obj]
                else:
                    self.prev_obj_clust_id = int(type_obj) + 6
                    self.clust_id = self.prev_obj_clust_id

                center_pos_array = np.array(self.act_cam_position)
                center_ori_array = np.array(self.act_cam_orientation)
                closest_array = np.array(closests_3d[1])
                # print(closests_3d[1])
                center_pos_str = np.array2string(np.array(center_pos_array), formatter={'float_kind':lambda x: "%.8f" % x}).replace(' ',',').replace('\n',',').replace(',,',',')
                center_ori_str = np.array2string(np.array(center_ori_array), formatter={'float_kind':lambda x: "%.8f" % x}).replace(' ',',').replace('\n',',').replace(',,',',')
                nearest_str = np.array2string(np.array(closest_array), formatter={'float_kind':lambda x: "%.8f" % x}).replace(' ',',').replace('\n',',').replace(',,',',')
                strTmapcam = center_pos_str + ' ' + center_ori_str

                # for near_p in nearest_obst_pts:
                json_data.append([self.list_of_ids[self.clust_id], strTmapcam, center_pos_str, nearest_str, type_obj])

            if len(json_data) > 0:
                df_json = pd.DataFrame(json_data, columns=["unique_id", "T_map_cam", "center_3d", "nearest_3d", "type"])
                self.json_human_workspace_pub.publish(str(df_json.to_json(orient='index')))
                

if __name__ == '__main__':
    
    ros_json_data = ROS2JsonData()

    rate = 100 # Hz
    ros_rate = rospy.Rate(rate)
			  
    while not rospy.is_shutdown():
        # ros_json_data.control_loop()
        ros_json_data.update()
        ros_rate.sleep()
