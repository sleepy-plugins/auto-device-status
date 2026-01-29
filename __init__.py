# coding: utf-8

import asyncio
import json
import os
import time
import argparse
from sqlmodel import Session, select
from loguru import logger as l

from plugin import PluginBase, PluginMetadata, plugin_manager
from main import engine, manager
import models as m

CONFIG_FILE = 'auto_status_config.json'
DEFAULT_TIMEOUT_MINUTES = 10

class Plugin(PluginBase):
    def __init__(self, metadata: PluginMetadata):
        super().__init__(metadata)
        self.config_path = os.path.join(os.path.dirname(__file__), CONFIG_FILE)
        self.task = None
        self.timeout_minutes = self._load_config()

    def on_load(self):
        l.info(f"{self.metadata.name} loaded. Timeout config: {self.timeout_minutes} min.")

    async def on_startup(self):
        l.info(f"{self.metadata.name} starting background check loop...")
        self.task = asyncio.create_task(self._check_loop())

    async def on_shutdown(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            l.info(f"{self.metadata.name} background task stopped.")

    def on_register_cli(self, subparsers: argparse._SubParsersAction):
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
        self.timeout_minutes = minutes # Update memory immediately
        with open(self.config_path, 'w') as f:
            json.dump({'timeout': minutes}, f)

    def handle_set_timeout(self, args):
        self._save_config(args.minutes)
        print(f"Timeout set to {args.minutes} minutes.")

    def handle_get_timeout(self, args):
        print(f"Current timeout: {self._load_config()} minutes")

    async def _check_loop(self):
        while True:
            try:
                await self._perform_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                l.error(f"Error in auto status check: {e}")
            
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
                    l.info(f"Device {dev.name} ({dev.id}) timed out.")
                    dev.using = False
                    dev.status = "Offline (Auto)"
                    sess.add(dev)
                    modified = True
                    timed_out_devices.append(dev.id)
                    
                    await manager.evt_broadcast('device_updated', {
                        'id': dev.id, 
                        'updated_fields': {'using': False, 'status': "Offline (Auto)"}
                    })

            if modified:
                sess.commit()
                if timed_out_devices:
                    await plugin_manager.trigger_hook('device_activity', device_ids=timed_out_devices, source='auto_timeout')
