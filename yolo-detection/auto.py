#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MapleStory Worlds 優化自動化系統
使用 YOLO 模型進行智能物件偵測和自動化操作
版本: 2.0
作者: AI Assistant
"""

import cv2
import mss
import numpy as np
import pyautogui
import time
import os
import sys
import logging
import yaml
import threading
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from ultralytics import YOLO

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('auto_system.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class Detection:
    """偵測結果數據類"""
    bbox: List[int]
    confidence: float
    class_id: int
    class_name: str
    center: Tuple[int, int]
    distance_from_center: float = 0.0

class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config = self.load_config()
    
    def load_config(self) -> Dict:
        """載入配置文件"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
            else:
                logger.warning(f"配置文件 {self.config_path} 不存在，使用默認配置")
                return self._get_default_config()
        except Exception as e:
            logger.error(f"載入配置失敗: {e}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict:
        """獲取默認配置"""
        return {
            'model': {
                'default_path': 'weights/best.pt',
                'confidence_threshold': 0.6,
                'iou_threshold': 0.45
            },
            'window': {
                'default': {'left': 100, 'top': 100, 'width': 1200, 'height': 800}
            },
            'controls': {
                'pickup_key': 'z',
                'interact_key': 'space',
                'attack_method': 'click'
            },
            'automation': {
                'action_delay': 0.3,
                'scan_interval': 0.1,
                'max_detection_distance': 200,
                'priority_targets': ['item', 'mob', 'npc']
            },
            'safety': {
                'enable_failsafe': True,
                'max_runtime_hours': 2
            }
        }
    
    def get(self, key_path: str, default=None):
        """獲取配置值，支持點分割路徑如 'model.confidence_threshold'"""
        try:
            keys = key_path.split('.')
            value = self.config
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default

class PerformanceMonitor:
    """性能監控器"""
    
    def __init__(self):
        self.fps_counter = 0
        self.last_fps_time = time.time()
        self.current_fps = 0
        self.detection_times = []
        
    def update_fps(self):
        """更新 FPS 計數"""
        self.fps_counter += 1
        current_time = time.time()
        if current_time - self.last_fps_time >= 1.0:
            self.current_fps = self.fps_counter
            self.fps_counter = 0
            self.last_fps_time = current_time
    
    def record_detection_time(self, detection_time: float):
        """記錄偵測時間"""
        self.detection_times.append(detection_time)
        if len(self.detection_times) > 100:  # 只保留最近100次
            self.detection_times.pop(0)
    
    def get_avg_detection_time(self) -> float:
        """獲取平均偵測時間"""
        return sum(self.detection_times) / len(self.detection_times) if self.detection_times else 0

class OptimizedMapleBot:
    """優化版 MapleStory 自動化機器人"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = ConfigManager(config_path)
        self.model = None
        self.running = False
        self.paused = False
        self.start_time = None
        self.performance_monitor = PerformanceMonitor()
        
        # 從配置載入設定
        self.monitor = self.config.get('window.default')
        # 閾值屬偵測行為設定；若未定義則回退到 model 區塊的舊值
        self.confidence_threshold = self.config.get(
            'detection_behavior.confidence_threshold',
            self.config.get('model.confidence_threshold', 0.6),
        )
        self.action_delay = self.config.get('automation.action_delay', 0.3)
        self.scan_interval = self.config.get('automation.scan_interval', 0.1)
        self.max_runtime = self.config.get('safety.max_runtime_hours', 2) * 3600
        
        # 統計數據
        self.stats = {
            'detections': 0,
            'actions_performed': 0,
            'items_collected': 0,
            'mobs_attacked': 0,
            'npcs_interacted': 0,
            'searches_performed': 0,
            'search_time_total': 0
        }
        
        # 尋找怪物相關變數
        self.last_mob_detection_time = time.time()
        self.is_searching = False
        self.search_start_time = 0
        self.original_position = None
        self.search_direction = 1  # 1 for right, -1 for left
        self.search_moves = 0

        # 角色位置穩定追蹤 (temporal smoothing)
        self._char_history: deque = deque(maxlen=8)
        self._char_smoothed: Optional[Detection] = None
        self._char_miss_frames = 0

        # Mob temporal confirmation removed: with the min-mob-box filter the
        # small item-as-mob flash boxes are already rejected, so a 3-frame
        # confirm would only delay attacks and can drop moving mobs.
        
        # 設定 PyAutoGUI
        if self.config.get('safety.enable_failsafe', True):
            pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05  # 減少暫停時間提升性能
        
        logger.info("OptimizedMapleBot 初始化完成")
        self._load_model()
    
    def _load_model(self):
        """載入 YOLO 模型"""
        model_path = self.config.get('model.default_path')
        if not model_path or not os.path.exists(model_path):
            logger.error(f"模型文件不存在: {model_path}")
            return False
        
        try:
            logger.info(f"載入模型: {model_path}")
            device = self.config.get('model.device', 'auto')
            if device == 'auto':
                import torch

                device = 'cuda' if torch.cuda.is_available() else 'cpu'
                logger.info(f"自動選擇設備: {device}")
            self.model = YOLO(model_path)
            if str(device).startswith('cuda'):
                self.model.to(device)
            self.model.conf = self.confidence_threshold
            self.model.iou = self.config.get('model.iou_threshold', 0.45)

            logger.info("✅ 模型載入成功!")
            logger.info(f"📊 模型類別: {self.model.names}")
            logger.info(f"🎮 模型設備: {device}")
            return True
            
        except Exception as e:
            logger.error(f"模型載入失敗: {e}")
            return False
    
    def capture_screen(self) -> Optional[np.ndarray]:
        """優化的螢幕擷取"""
        try:
            with mss.mss() as sct:
                screenshot = sct.grab(self.monitor)
                img = np.array(screenshot)
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                return img
        except Exception as e:
            logger.error(f"螢幕擷取失敗: {e}")
            return None
    
    def detect_objects(
        self, img: np.ndarray, include_all: bool = False
    ) -> List[Detection]:
        """Detect objects in the frame.

        Default: mobs only, restricted to the center zone, passed through
        the temporal-confirmation tracker (used by the attack decision).
        With ``include_all=True`` character/environment/mob detections are
        returned without the zone filter or tracker, so the preview can
        mark those classes with their own colors (item/npc/ui are
        excluded).
        """
        if self.model is None:
            return []

        start_time = time.time()

        # 中央區域限制 (僅在畫面中央偵測)
        center_zone = self.config.get('detection_behavior.center_zone', {})
        zone_enabled = bool(center_zone.get('enabled', False))
        zone_w = float(center_zone.get('width_fraction', 0.6))
        zone_h = float(center_zone.get('height_fraction', 0.6))
        # Vertical shift of the zone center as a fraction of frame height;
        # positive moves the zone down, negative moves it up.
        zone_shift_y = float(center_zone.get('shift_y', 0.0))
        zone_shift_y = max(-0.5, min(0.5, zone_shift_y))
        zone_w = max(0.1, min(1.0, zone_w))
        zone_h = max(0.1, min(1.0, zone_h))
        zone_left = int((self.monitor['width'] * (1 - zone_w)) / 2)
        zone_top = int(self.monitor['height'] * ((1 - zone_h) / 2 + zone_shift_y))
        zone_right = self.monitor['width'] - zone_left
        zone_bottom = int(self.monitor['height'] * ((1 + zone_h) / 2 + zone_shift_y))
        zone_top = max(0, min(zone_top, zone_bottom - 1))
        zone_bottom = max(zone_top + 1, min(self.monitor['height'], zone_bottom))

        # 只偵測 mob 的開關
        detect_only_mobs = bool(self.config.get('detection_behavior.detect_only_mobs', True))

        try:
            results = self.model(
                img, conf=self.confidence_threshold, verbose=False
            )
            detections = []

            # 計算畫面中心點
            center_x, center_y = self.monitor['width'] // 2, self.monitor['height'] // 2

            for result in results:
                boxes = result.boxes
                if boxes is not None:
                    for box in boxes:
                        xyxy = box.xyxy[0].cpu().numpy()
                        conf = box.conf[0].cpu().numpy()
                        cls = box.cls[0].cpu().numpy()

                        if conf > self.confidence_threshold:
                            class_name = self.model.names[int(cls)]
                            # 只保留 mob (preview mode keeps character/
                            # environment/mob only)
                            if detect_only_mobs and not include_all and class_name != 'mob':
                                continue
                            if (include_all
                                    and class_name not in
                                    ('character', 'environment', 'mob')):
                                continue
                            detection_center = (int((xyxy[0] + xyxy[2]) / 2), int((xyxy[1] + xyxy[3]) / 2))
                            # 只保留中央區域內的偵測 (full-class preview
                            # ignores the zone so every class is visible)
                            if zone_enabled and not include_all:
                                if not (zone_left <= detection_center[0] <= zone_right
                                        and zone_top <= detection_center[1] <= zone_bottom):
                                    continue

                            # 計算距離中心點的距離
                            distance = np.sqrt((detection_center[0] - center_x)**2 + (detection_center[1] - center_y)**2)

                            detection = Detection(
                                bbox=[int(x) for x in xyxy],
                                confidence=float(conf),
                                class_id=int(cls),
                                class_name=class_name,
                                center=detection_center,
                                distance_from_center=distance
                            )
                            detections.append(detection)

            # 按優先級和距離排序
            detections = self._prioritize_detections(detections)

            # 記錄統計
            self.stats['detections'] += len(detections)
            detection_time = time.time() - start_time
            self.performance_monitor.record_detection_time(detection_time)

            return detections

        except Exception as e:
            logger.error(f"物件偵測失敗: {e}")
            return []

    @staticmethod
    def _score_character_candidates(
        candidates: List[Detection],
        previous: Optional[Tuple[int, int]],
        monitor_width: int,
        monitor_height: int,
    ) -> Detection:
        """Score character candidates; returns the best one.

        The camera follows the player in MapleStory, so the real player is
        almost always near the screen center.  Screen-center proximity is
        therefore the dominant signal; continuity with the previously accepted
        position is a secondary tie-breaker (it follows smooth motion and
        rejects flickering false positives), and raw confidence is last.
        """

        center_ref = (monitor_width / 2.0, monitor_height * 0.55)
        max_ref_dist = float(np.hypot(monitor_width, monitor_height) / 2.0)

        def score(candidate: Detection) -> float:
            # Center proximity: 1.0 at screen center -> 0.0 at the corners.
            ref_dist = float(np.hypot(
                candidate.center[0] - center_ref[0],
                candidate.center[1] - center_ref[1],
            ))
            center_score = max(0.0, 1.0 - ref_dist / max(1.0, max_ref_dist))
            s = 1.2 * center_score
            # Continuity: bonus for being near the last accepted position.
            if previous is not None:
                dx = candidate.center[0] - previous[0]
                dy = candidate.center[1] - previous[1]
                dist = float(np.hypot(dx, dy))
                continuity = max(
                    0.0, 1.0 - dist / max(1.0, monitor_width * 0.20)
                )
                s += 0.5 * continuity
            # Confidence: mild final tie-breaker.
            s += 0.2 * candidate.confidence
            return s

        return max(candidates, key=score)

    def detect_character(self, img: np.ndarray) -> Optional[Detection]:
        """Return a temporally stabilized player character position.

        Runs the model without the mob-only filter and collects all
        ``character`` boxes.  The true player is tracked across frames:
        candidates near the previously accepted position are preferred over
        high-confidence false positives (NPCs/decorations), and the accepted
        center is a median over recent frames so the reported position does
        not jump.  When the character is briefly missed the last known
        position is kept for a few frames.
        """

        if self.model is None:
            return None
        try:
            results = self.model(
                img, conf=self.confidence_threshold, verbose=False
            )
            candidates = []
            center_x, center_y = self.monitor['width'] // 2, self.monitor['height'] // 2
            for result in results:
                boxes = result.boxes
                if boxes is None:
                    continue
                for box in boxes:
                    cls = int(box.cls[0])
                    if self.model.names[cls] != 'character':
                        continue
                    conf = float(box.conf[0])
                    if conf < self.confidence_threshold:
                        continue
                    xyxy = [int(v) for v in box.xyxy[0].cpu().numpy().tolist()]
                    detection_center = ((xyxy[0] + xyxy[2]) // 2,
                                        (xyxy[1] + xyxy[3]) // 2)
                    distance = np.sqrt(
                        (detection_center[0] - center_x) ** 2
                        + (detection_center[1] - center_y) ** 2
                    )
                    candidates.append(Detection(
                        bbox=xyxy, confidence=conf, class_id=cls,
                        class_name='character', center=detection_center,
                        distance_from_center=float(distance),
                    ))

            if not candidates:
                # Brief miss: keep the last known position for a few frames.
                self._char_miss_frames += 1
                if (self._char_smoothed is not None
                        and self._char_miss_frames <= 5):
                    return self._char_smoothed
                self._char_smoothed = None
                return None
            self._char_miss_frames = 0

            # Score candidates: continuity with the last accepted position is
            # the strongest signal; confidence and screen-center proximity
            # break ties.
            previous = (self._char_smoothed.center
                        if self._char_smoothed is not None else None)
            best = self._score_character_candidates(
                candidates, previous,
                self.monitor['width'], self.monitor['height'],
            )

            # Median-smooth the center over recent frames to kill jitter.
            self._char_history.append(best.center)
            xs = [p[0] for p in self._char_history]
            ys = [p[1] for p in self._char_history]
            smoothed_x = int(sorted(xs)[len(xs) // 2])
            smoothed_y = int(sorted(ys)[len(ys) // 2])
            best = Detection(
                bbox=best.bbox, confidence=best.confidence, class_id=best.class_id,
                class_name=best.class_name, center=(smoothed_x, smoothed_y),
                distance_from_center=best.distance_from_center,
            )
            self._char_smoothed = best
            return best
        except Exception as e:
            logger.error(f"角色偵測失敗: {e}")
            return None

    def detect_rope(
        self, img: np.ndarray, min_height: int = 80
    ) -> Optional[Detection]:
        """Find the rope (tall narrow environment box) near the character.

        Ropes in MapleStory are tall, thin ``environment`` detections.  We
        return the rope-like box whose center X is closest to the character
        (or screen center when the character is not visible).  Used to gate
        the inner-gap jump on the real screen gap instead of the minimap
        estimate.
        """

        if self.model is None:
            return None
        try:
            results = self.model(
                img, conf=self.confidence_threshold, verbose=False
            )
        except Exception as e:
            logger.error(f"rope 偵測失敗: {e}")
            return None
        character = self.detect_character(img)
        char_x = (
            character.center[0]
            if character is not None
            else self.monitor['width'] // 2
        )
        candidates = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            for box in boxes:
                cls = int(box.cls[0].cpu().numpy())
                if self.model.names[cls] != 'environment':
                    continue
                conf = float(box.conf[0].cpu().numpy())
                if conf < self.confidence_threshold:
                    continue
                xyxy = [int(v) for v in box.xyxy[0].cpu().numpy().tolist()]
                x1, y1, x2, y2 = xyxy
                width = x2 - x1
                height = y2 - y1
                # Rope-like: clearly taller than wide, with a real height.
                if height < min_height or height < 2.5 * width:
                    continue
                center = ((x1 + x2) // 2, (y1 + y2) // 2)
                candidates.append((abs(center[0] - char_x), Detection(
                    bbox=xyxy, confidence=conf, class_id=cls,
                    class_name='environment', center=center,
                    distance_from_center=float(abs(center[0] - char_x)),
                )))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def attack_decision(
        self, mobs: List[Detection], character: Optional[Detection],
        attack_range: int = 800,
    ) -> Optional[Detection]:
        """Return the best mob to attack, or None.

        ``attack_range`` is the width of the attack range line drawn on the
        preview, so a mob is attackable only when its center is within
        ``attack_range/2`` pixels horizontally of the character (the line
        spans half the range on each side) and within ``attack_range/2``
        vertically.  This keeps the decision identical to the visible line:
        a mob outside the line is never attacked.  Among attackable mobs the
        one nearest the character wins.  Without a character position no mob
        is attackable.

        Mobs smaller than ``detection_behavior.min_mob_box_px`` on either
        side are ignored: dropped items are frequently misclassified as
        mobs, and those boxes are tiny.
        """

        if character is None or not mobs:
            return None
        config = getattr(self, "config", {}) or {}
        min_box = float(config.get(
            'detection_behavior.min_mob_box_px', 20.0
        ))
        cx, cy = character.center
        half = max(10.0, float(attack_range) / 2.0)
        attackable = []
        for mob in mobs:
            x1, y1, x2, y2 = mob.bbox
            if (x2 - x1) < min_box or (y2 - y1) < min_box:
                continue  # too small: likely a misclassified drop
            mx, my = mob.center
            dx = mx - cx
            dy = my - cy
            if abs(dx) <= half and abs(dy) <= half:
                distance = float(np.hypot(dx, dy))
                attackable.append((distance, mob))
        if not attackable:
            return None
        attackable.sort(key=lambda item: item[0])
        return attackable[0][1]
    def _prioritize_detections(self, detections: List[Detection]) -> List[Detection]:
        """按優先級和距離排序偵測結果"""
        priority_map = {name: i for i, name in enumerate(self.config.get('automation.priority_targets', []))}
        
        def sort_key(detection):
            priority = priority_map.get(detection.class_name, 999)
            return (priority, detection.distance_from_center)
        
        return sorted(detections, key=sort_key)
    
    def perform_action(self, detection: Detection) -> bool:
        """執行優化的遊戲動作"""
        class_name = detection.class_name
        abs_x = self.monitor['left'] + detection.center[0]
        abs_y = self.monitor['top'] + detection.center[1]
        
        # 檢查距離限制
        max_distance = self.config.get(f'detection_behavior.{class_name}.max_distance', 200)
        if detection.distance_from_center > max_distance:
            return False
        
        try:
            action_performed = False
            
            if class_name == 'mob':
                # 檢查是否啟用攻擊動作
                mob_action = self.config.get('detection_behavior.mob.action', 'attack')
                if mob_action == 'attack':
                    pyautogui.moveTo(abs_x, abs_y, duration=0.1)
                    attack_method = self.config.get('controls.attack_method', 'click')
                    if attack_method == 'key':
                        attack_key = self.config.get('controls.attack_key', 'z')
                        pyautogui.press(attack_key)
                    else:
                        pyautogui.click()
                    logger.info(f"⚔️ 攻擊怪物 (信賴度: {detection.confidence:.2f})")
                    self.stats['mobs_attacked'] += 1
                    action_performed = True
                    time.sleep(self.config.get('detection_behavior.mob.attack_delay', 0.5))
                else:
                    logger.info(f"👁️ 偵測到怪物 (信賴度: {detection.confidence:.2f}) - 僅記錄")
                
            elif class_name == 'item':
                # 只偵測物品，不執行動作
                logger.info(f"👁️ 偵測到物品 (信賴度: {detection.confidence:.2f}) - 僅記錄")
                
            elif class_name == 'npc':
                # 只偵測 NPC，不執行動作
                logger.info(f"👁️ 偵測到 NPC (信賴度: {detection.confidence:.2f}) - 僅記錄")
            
            if action_performed:
                self.stats['actions_performed'] += 1
                return True
                
        except Exception as e:
            logger.error(f"執行動作失敗: {e}")
        
        return False
    
    def _should_search_for_mobs(self) -> bool:
        """檢查是否應該開始尋找怪物"""
        if not self.config.get('automation.mob_hunting.enable', True):
            return False
        
        # 如果正在搜尋中，不重複開始
        if self.is_searching:
            return False
        
        # 檢查距離上次偵測到怪物的時間
        search_delay = self.config.get('automation.mob_hunting.search_delay', 2.0)
        time_since_last_mob = time.time() - self.last_mob_detection_time
        
        return time_since_last_mob > search_delay
    
    def _start_mob_search(self):
        """開始尋找怪物"""
        if self.is_searching:
            return
        
        self.is_searching = True
        self.search_start_time = time.time()
        self.search_moves = 0
        
        # 記錄當前位置（假設角色在畫面中心）
        self.original_position = (self.monitor['width'] // 2, self.monitor['height'] // 2)
        
        logger.info("🔍 開始尋找怪物...")
    
    def _perform_mob_search(self):
        """執行尋找怪物的移動"""
        if not self.is_searching:
            return
        
        max_search_time = self.config.get('automation.mob_hunting.max_search_time', 10)
        if time.time() - self.search_start_time > max_search_time:
            self._end_mob_search()
            return
        
        search_pattern = self.config.get('automation.mob_hunting.search_pattern', 'horizontal')
        move_distance = self.config.get('automation.mob_hunting.move_distance', 100)
        
        try:
            if search_pattern == 'horizontal':
                self._horizontal_search(move_distance)
            elif search_pattern == 'vertical':
                self._vertical_search(move_distance)
            elif search_pattern == 'random':
                self._random_search(move_distance)
            
            time.sleep(0.5)  # 移動後稍作停頓
            
        except Exception as e:
            logger.error(f"搜尋移動失敗: {e}")
            self._end_mob_search()
    
    def _horizontal_search(self, move_distance: int):
        """水平搜尋移動"""
        move_key = self.config.get('controls.movement_keys.right' if self.search_direction > 0 else 'controls.movement_keys.left', 'right' if self.search_direction > 0 else 'left')
        
        # 按住移動鍵一段時間
        pyautogui.keyDown(move_key)
        time.sleep(0.3)
        pyautogui.keyUp(move_key)
        
        self.search_moves += 1
        
        # 每移動3次改變方向
        if self.search_moves >= 3:
            self.search_direction *= -1
            self.search_moves = 0
            logger.info(f"🔄 改變搜尋方向: {'右' if self.search_direction > 0 else '左'}")
    
    def _vertical_search(self, move_distance: int):
        """垂直搜尋移動（跳躍和下降）"""
        if self.search_moves % 2 == 0:
            # 跳躍
            jump_key = self.config.get('controls.movement_keys.jump', 'x')
            pyautogui.press(jump_key)
            logger.info("⬆️ 跳躍搜尋")
        else:
            # 向下移動
            down_key = self.config.get('controls.movement_keys.down', 'down')
            pyautogui.keyDown(down_key)
            time.sleep(0.2)
            pyautogui.keyUp(down_key)
            logger.info("⬇️ 向下搜尋")
        
        self.search_moves += 1
    
    def _random_search(self, move_distance: int):
        """隨機搜尋移動"""
        import random
        
        movements = ['left', 'right', 'jump']
        chosen_movement = random.choice(movements)
        
        if chosen_movement == 'jump':
            jump_key = self.config.get('controls.movement_keys.jump', 'x')
            pyautogui.press(jump_key)
            logger.info("🎲 隨機跳躍")
        else:
            move_key = self.config.get(f'controls.movement_keys.{chosen_movement}', chosen_movement)
            pyautogui.keyDown(move_key)
            time.sleep(0.3)
            pyautogui.keyUp(move_key)
            logger.info(f"🎲 隨機移動: {chosen_movement}")
        
        self.search_moves += 1
    
    def _end_mob_search(self):
        """結束尋找怪物"""
        if not self.is_searching:
            return
        
        # 記錄搜尋統計
        search_duration = time.time() - self.search_start_time
        self.stats['searches_performed'] += 1
        self.stats['search_time_total'] += search_duration
        
        self.is_searching = False
        logger.info(f"🏁 結束怪物搜尋 (耗時: {search_duration:.1f}秒)")
        
        # 如果設定要返回中心，執行返回動作
        if self.config.get('automation.mob_hunting.return_to_center', True):
            self._return_to_center()
    
    def _return_to_center(self):
        """返回到搜尋開始的位置"""
        try:
            logger.info("🏠 返回原始位置...")
            # 簡單的返回邏輯：向相反方向移動
            if self.search_direction > 0:
                # 如果最後是向右移動，現在向左移動
                move_key = self.config.get('controls.movement_keys.left', 'left')
            else:
                # 如果最後是向左移動，現在向右移動
                move_key = self.config.get('controls.movement_keys.right', 'right')
            
            pyautogui.keyDown(move_key)
            time.sleep(0.5)  # 移動時間稍長一些
            pyautogui.keyUp(move_key)
            
        except Exception as e:
            logger.error(f"返回中心失敗: {e}")
    
    def _check_safety_conditions(self) -> bool:
        """檢查安全條件"""
        if self.start_time and time.time() - self.start_time > self.max_runtime:
            logger.warning("達到最大運行時間限制")
            return False
        return True
    
    def start_automation(self, show_preview: bool = False):
        """開始優化的自動化流程"""
        if self.model is None:
            logger.error("模型未載入，無法開始自動化")
            return
        
        self.running = True
        self.start_time = time.time()
        logger.info("🚀 開始 MapleStory Worlds 優化自動化")
        logger.info("按 'q' 鍵暫停/恢復，'Esc' 鍵停止")
        
        last_stats_time = time.time()
        
        try:
            while self.running:
                if not self._check_safety_conditions():
                    break
                
                if self.paused:
                    time.sleep(0.1)
                    continue
                
                # 擷取和偵測
                img = self.capture_screen()
                if img is None:
                    continue
                
                detections = self.detect_objects(img)
                
                # 檢查是否偵測到怪物，更新最後偵測時間
                mob_detected = any(d.class_name == 'mob' for d in detections)
                if mob_detected:
                    self.last_mob_detection_time = time.time()
                    # 如果正在搜尋中且偵測到怪物，停止搜尋
                    if self.is_searching:
                        self._end_mob_search()
                
                # 執行動作
                actions_this_cycle = 0
                for detection in detections:
                    if not self.running or self.paused:
                        break
                    
                    if self.perform_action(detection):
                        actions_this_cycle += 1
                        if actions_this_cycle >= 3:  # 限制每週期最多執行3個動作
                            break
                        time.sleep(self.action_delay)
                
                # 如果沒有偵測到怪物且不在搜尋中，檢查是否需要開始搜尋
                if not mob_detected and self._should_search_for_mobs():
                    self._start_mob_search()
                
                # 如果正在搜尋中，執行搜尋移動
                if self.is_searching:
                    self._perform_mob_search()
                
                # 顯示預覽
                if show_preview and detections:
                    preview_img = self._draw_detections(img.copy(), detections)
                    cv2.imshow('MapleStory Auto Bot - 按 q 暫停/恢復', preview_img)
                
                # 更新性能監控
                self.performance_monitor.update_fps()
                
                # 定期顯示統計
                if time.time() - last_stats_time >= 30:  # 每30秒顯示一次
                    self._log_statistics()
                    last_stats_time = time.time()
                
                # 檢查按鍵
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    self.paused = not self.paused
                    logger.info(f"{'⏸️ 暫停' if self.paused else '▶️ 恢復'}自動化")
                elif key == 27:  # Esc
                    break
                
                time.sleep(self.scan_interval)
                
        except KeyboardInterrupt:
            logger.info("⏹️ 使用者中斷自動化")
        except Exception as e:
            logger.error(f"自動化過程中發生錯誤: {e}")
        finally:
            self.running = False
            cv2.destroyAllWindows()
            self._log_final_statistics()
            logger.info("✅ 自動化已停止")
    
    def _draw_detections(self, img: np.ndarray, detections: List[Detection]) -> np.ndarray:
        """繪製偵測結果"""
        height, width = img.shape[:2]

        # 繪製中央偵測區域
        center_zone = self.config.get('detection_behavior.center_zone', {})
        if center_zone.get('enabled', False):
            zone_w = float(center_zone.get('width_fraction', 0.6))
            zone_h = float(center_zone.get('height_fraction', 0.6))
            zone_shift_y = float(center_zone.get('shift_y', 0.0))
            zone_shift_y = max(-0.5, min(0.5, zone_shift_y))
            zone_w = max(0.1, min(1.0, zone_w))
            zone_h = max(0.1, min(1.0, zone_h))
            zx1 = int((width * (1 - zone_w)) / 2)
            zy1 = int(height * ((1 - zone_h) / 2 + zone_shift_y))
            zx2 = width - zx1
            zy2 = int(height * ((1 + zone_h) / 2 + zone_shift_y))
            zy1 = max(0, min(zy1, zy2 - 1))
            zy2 = max(zy1 + 1, min(height, zy2))
            cv2.rectangle(img, (zx1, zy1), (zx2, zy2), (0, 255, 255), 1)
            cv2.putText(img, 'CENTER ZONE', (zx1 + 5, zy1 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

        for detection in detections:
            bbox = detection.bbox
            class_name = detection.class_name
            confidence = detection.confidence

            # 根據類型設定顏色
            color_map = {
                'mob': (0, 0, 255),      # 紅色
                'item': (0, 255, 0),     # 綠色
                'npc': (255, 0, 0),      # 藍色
                'character': (255, 255, 0), # 青色
                'environment': (128, 128, 128), # 灰色
                'ui': (255, 0, 255)      # 洋紅色
            }
            color = color_map.get(class_name, (255, 255, 255))

            # 繪製邊界框
            cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)

            # 繪製標籤
            label = f"{class_name}: {confidence:.2f}"
            cv2.putText(img, label, (bbox[0], bbox[1] - 10), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # 繪製性能信息
        fps_text = f"FPS: {self.performance_monitor.current_fps}"
        cv2.putText(img, fps_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        return img

    def _log_statistics(self):
        """記錄統計信息"""
        runtime = time.time() - self.start_time if self.start_time else 0
        avg_detection_time = self.performance_monitor.get_avg_detection_time()
        
        logger.info("📊 運行統計:")
        logger.info(f"   運行時間: {runtime/60:.1f} 分鐘")
        logger.info(f"   FPS: {self.performance_monitor.current_fps}")
        logger.info(f"   平均偵測時間: {avg_detection_time*1000:.1f}ms")
        logger.info(f"   總偵測次數: {self.stats['detections']}")
        logger.info(f"   執行動作: {self.stats['actions_performed']}")
        logger.info(f"   撿取物品: {self.stats['items_collected']}")
        logger.info(f"   攻擊怪物: {self.stats['mobs_attacked']}")
        logger.info(f"   NPC互動: {self.stats['npcs_interacted']}")
        logger.info(f"   搜尋次數: {self.stats['searches_performed']}")
        if self.stats['searches_performed'] > 0:
            avg_search_time = self.stats['search_time_total'] / self.stats['searches_performed']
            logger.info(f"   平均搜尋時間: {avg_search_time:.1f}秒")
    
    def _log_final_statistics(self):
        """記錄最終統計"""
        logger.info("🎯 最終統計報告:")
        self._log_statistics()
    
    def get_performance_summary(self) -> Dict:
        """獲取性能摘要"""
        runtime = time.time() - self.start_time if self.start_time else 0
        avg_detection_time = self.performance_monitor.get_avg_detection_time()
        
        return {
            'runtime_minutes': runtime / 60,
            'current_fps': self.performance_monitor.current_fps,
            'avg_detection_time_ms': avg_detection_time * 1000,
            'total_detections': self.stats['detections'],
            'actions_performed': self.stats['actions_performed'],
            'items_collected': self.stats['items_collected'],
            'mobs_attacked': self.stats['mobs_attacked'],
            'npcs_interacted': self.stats['npcs_interacted'],
            'searches_performed': self.stats['searches_performed'],
            'avg_search_time': self.stats['search_time_total'] / max(1, self.stats['searches_performed'])
        }
    
    def test_detection(self):
        """測試偵測功能"""
        if self.model is None:
            logger.error("模型未載入")
            return
        
        logger.info("🧪 測試物件偵測功能")
        img = self.capture_screen()
        if img is None:
            logger.error("無法擷取畫面")
            return
        
        detections = self.detect_objects(img)
        logger.info(f"📊 偵測結果: 發現 {len(detections)} 個物件")
        
        for i, detection in enumerate(detections, 1):
            logger.info(f"  {i}. {detection.class_name} (信賴度: {detection.confidence:.2f}, 距離: {detection.distance_from_center:.0f}px)")
        
        if detections:
            result_img = self._draw_detections(img, detections)
            cv2.imshow('Detection Test', result_img)
            logger.info("按任意鍵關閉預覽視窗")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        else:
            logger.info("未偵測到任何物件")

def load_available_models() -> Dict[str, str]:
    """載入可用的模型文件"""
    models = {}
    weights_dir = Path("weights")
    
    if weights_dir.exists():
        for i, model_file in enumerate(weights_dir.glob("*.pt"), 1):
            size_mb = model_file.stat().st_size / (1024 * 1024)
            models[str(i)] = str(model_file)
            print(f"  {i}. {model_file} ({size_mb:.1f} MB)")
    
    return models

def main():
    """主程序"""
    print("🍁 MapleStory Worlds 優化自動化系統 v2.0")
    print("=" * 60)
    
    # 檢查配置文件
    if not os.path.exists("config.yaml"):
        logger.warning("配置文件不存在，將使用默認設定")
    
    # 顯示可用模型
    print("可用的模型文件:")
    models = load_available_models()
    
    if not models:
        logger.error("未找到任何模型文件")
        return
    
    # 選擇模型
    choice = input(f"\n請選擇模型文件 (1-{len(models)}, 預設1): ").strip()
    if choice not in models:
        choice = '1'
    
    model_path = models[choice]
    if not os.path.exists(model_path):
        logger.error(f"選擇的模型文件不存在: {model_path}")
        return
    
    # 創建配置並設定模型路徑
    config = ConfigManager()
    config.config['model']['default_path'] = model_path
    
    # 創建機器人
    bot = OptimizedMapleBot()
    
    # 主選單
    while True:
        print("\n🎮 功能選單:")
        print("1. 測試物件偵測")
        print("2. 開始自動化 (有預覽)")
        print("3. 開始自動化 (無預覽)")
        print("4. 調整視窗設定")
        print("5. 查看配置")
        print("6. 查看統計")
        print("7. 退出")
        
        choice = input("\n請選擇功能 (1-7): ").strip()
        
        if choice == '1':
            bot.test_detection()
        elif choice == '2':
            bot.start_automation(show_preview=True)
        elif choice == '3':
            bot.start_automation(show_preview=False)
        elif choice == '4':
            _adjust_window_settings(bot)
        elif choice == '5':
            _show_config(bot.config)
        elif choice == '6':
            bot._log_statistics()
        elif choice == '7':
            break
        else:
            print("❌ 無效選擇")
    
    print("👋 再見！")

def _adjust_window_settings(bot):
    """調整視窗設定"""
    print(f"\n當前視窗設定:")
    print(f"  左上角: ({bot.monitor['left']}, {bot.monitor['top']})")
    print(f"  大小: {bot.monitor['width']} x {bot.monitor['height']}")
    
    # 提供預設選項
    print("\n預設選項:")
    print("1. Full HD (1920x1080)")
    print("2. QHD (2560x1440)")
    print("3. 自訂設定")
    
    preset_choice = input("選擇預設或自訂 (1-3): ").strip()
    
    if preset_choice == '1':
        bot.monitor = {'left': 0, 'top': 100, 'width': 1920, 'height': 980}
    elif preset_choice == '2':
        bot.monitor = {'left': 320, 'top': 180, 'width': 1280, 'height': 720}
    elif preset_choice == '3':
        try:
            bot.monitor['left'] = int(input("請輸入左側位置: ") or bot.monitor['left'])
            bot.monitor['top'] = int(input("請輸入頂部位置: ") or bot.monitor['top'])
            bot.monitor['width'] = int(input("請輸入寬度: ") or bot.monitor['width'])
            bot.monitor['height'] = int(input("請輸入高度: ") or bot.monitor['height'])
        except ValueError:
            print("❌ 輸入格式錯誤")
            return
    
    print("✅ 視窗設定已更新")

def _show_config(config: ConfigManager):
    """顯示當前配置"""
    print("\n⚙️ 當前配置:")
    print(f"  模型路徑: {config.get('model.default_path')}")
    print(f"  信賴度閾值: {config.get('model.confidence_threshold')}")
    print(f"  動作延遲: {config.get('automation.action_delay')}秒")
    print(f"  掃描間隔: {config.get('automation.scan_interval')}秒")
    print(f"  最大運行時間: {config.get('safety.max_runtime_hours')}小時")
    print(f"  撿取鍵: {config.get('controls.pickup_key')}")
    print(f"  互動鍵: {config.get('controls.interact_key')}")

if __name__ == "__main__":
    main() 