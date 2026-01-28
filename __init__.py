# coding: utf-8

import asyncio
import json
import os
import time
import argparse
from sqlmodel import Session, select
from loguru import logger as l

from plugin import PluginBase, PluginMetadata, plugin_manager
from main import engine, manager # 导入数据库引擎和 websocket manager
import models as m

CONFIG_FILE = 'data/auto_status_config.json'
DEFAULT_TIMEOUT_MINUTES = 10

class Plugin(PluginBase):
    def __init__(self, metadata: PluginMetadata):
        super().__init__(metadata)
        self.config_path = os.path.join(os.path.dirname(__file__), CONFIG_FILE)
        self.task = None
        self.timeout_minutes = self._load_config()

    def on_load(self):
        l.info(f"{self.metadata.name} loaded. Timeout: {self.timeout_minutes} min.")
        # 启动后台任务
        self.task = asyncio.create_task(self._check_loop())

    def on_unload(self):
        # 取消后台任务
        if self.task:
            self.task.cancel()
            l.info(f"{self.metadata.name} background task cancelled.")

    def on_register_cli(self, subparsers: argparse._SubParsersAction):
        # 注册 CLI 命令来配置超时时间
        # uv run main.py auto-status set-timeout 15
        parser = subparsers.add_parser('auto-status', help='Configure auto device status')
        sub = parser.add_subparsers(dest='action', required=True)

        p_set = sub.add_parser('set-timeout', help='Set offline timeout in minutes')
        p_set.add_argument('minutes', type=int, help='Minutes')
        p_set.set_defaults(func=self.handle_set_timeout)

        p_get = sub.add_parser('get-timeout', help='Show current timeout')
        p_get.set_defaults(func=self.handle_get_timeout)

    def _load_config(self) -> int:
        if not os.path.exists(self.config_path):
            return DEFAULT_TIMEOUT_MINUTES
        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
                return data.get('timeout', DEFAULT_TIMEOUT_MINUTES)
        except:
            return DEFAULT_TIMEOUT_MINUTES

    def _save_config(self, minutes: int):
        with open(self.config_path, 'w') as f:
            json.dump({'timeout': minutes}, f)

    def handle_set_timeout(self, args):
        self._save_config(args.minutes)
        print(f"Timeout set to {args.minutes} minutes. Restart server to apply immediately, or wait for next loop.")

    def handle_get_timeout(self, args):
        print(f"Current timeout: {self._load_config()} minutes")

    async def _check_loop(self):
        """后台循环检查任务"""
        l.info("Auto-device-status loop started.")
        while True:
            try:
                await self._perform_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                l.error(f"Error in auto status check: {e}")
            
            # 每 60 秒检查一次
            await asyncio.sleep(60)

    async def _perform_check(self):
        timeout_seconds = self.timeout_minutes * 60
        now = time.time()
        
        with Session(engine) as sess:
            devices = sess.exec(select(m.DeviceData).where(m.DeviceData.using == True)).all()
            
            modified = False
            timed_out_devices = []

            for dev in devices:
                if now - dev.last_updated > timeout_seconds:
                    l.info(f"Device {dev.name} ({dev.id}) timed out. Marking offline.")
                    dev.using = False
                    dev.status = "Offline (Auto)"
                    sess.add(dev)
                    modified = True
                    timed_out_devices.append(dev.id)

                    await manager.evt_broadcast('device_updated', {
                        'id': dev.id, 
                        'updated_fields': {'using': False, 'status': "Offline (Auto)"}
                    })

                    await manager.evt_broadcast('device_updated', {
                        'id': dev.id, 
                        'updated_fields': {'using': False, 'status': "Offline (Auto)"}
                    })

            if modified:
                sess.commit()
                if timed_out_devices:
                    await plugin_manager.trigger_hook('device_activity', device_ids=timed_out_devices, source='auto_timeout')
