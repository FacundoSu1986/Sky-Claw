"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     SKY-CLAW GUI v2.0 — REFACTORED                       ║
║         Isolation & Event-Driven Architecture (2026)                      ║
╚══════════════════════════════════════════════════════════════════════════════╝

Refactored from sky_claw_production (3).py:
  • REMOVED duplicate DatabaseManager → uses sky_claw.core.database
  • EXTRACTED CSS → frontend/css/sky_claw_styles.css
  • FIRE-AND-FORGET → process_user_message sends via AgentCommunicationClient
  • HOST 0.0.0.0 → accessible from Windows when running in WSL2
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List

from nicegui import ui, app

# ── Core imports (consolidated data layer) ──
from sky_claw.core.database import DatabaseAgent
from sky_claw.gui.agent_communication import AgentCommunicationClient

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN Y CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════════

COLORS = {
    'bg_primary': '#000000',
    'bg_secondary': '#0a0a0a',
    'bg_card': '#0f0f0f',
    'border': '#1f2937',
    'border_hover': '#374151',
    'accent_violet': '#8b5cf6',
    'accent_cyan': '#06b6d4',
    'accent_pink': '#ec4899',
    'accent_blue': '#3b82f6',
    'text_primary': '#ffffff',
    'text_secondary': '#9ca3af',
    'text_muted': '#6b7280',
    'glow_violet': 'rgba(139, 92, 246, 0.4)',
    'glow_cyan': 'rgba(6, 182, 212, 0.3)',
}

# Resolve paths relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CSS_PATH = _PROJECT_ROOT / "frontend" / "css" / "sky_claw_styles.css"


# ═══════════════════════════════════════════════════════════════════════════════
# SISTEMA DE EVENTOS — PATRÓN OBSERVER
# ═══════════════════════════════════════════════════════════════════════════════

class EventType(Enum):
    MOD_ADDED = "mod_added"
    MOD_REMOVED = "mod_removed"
    MOD_UPDATED = "mod_updated"
    CONFLICT_DETECTED = "conflict_detected"
    CONFLICT_RESOLVED = "conflict_resolved"
    LLM_RESPONSE = "llm_response"
    DOWNLOAD_PROGRESS = "download_progress"
    AGENT_STATUS_CHANGE = "agent_status_change"
    CONFIG_CHANGED = "config_changed"
    # New: broadcast from daemon via WS
    EVENT_BROADCAST = "event_broadcast"


@dataclass
class SkyClawEvent:
    type: EventType
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "system"


class EventBus:
    """Thread-safe singleton event bus (Observer pattern)."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)
        self._subscribers_lock = threading.Lock()
        self._event_queue: queue.Queue = queue.Queue()
        self._running = False
        self._logger = logging.getLogger("SkyClaw.EventBus")

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            if callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    def publish(self, event: SkyClawEvent) -> None:
        self._event_queue.put(event)
        self._logger.info(f"Evento publicado: {event.type.value}")

    def _process_events(self) -> None:
        while self._running:
            try:
                event = self._event_queue.get(timeout=0.1)
                with self._subscribers_lock:
                    callbacks = list(self._subscribers.get(event.type, []))
                for callback in callbacks:
                    try:
                        # Marshal to main asyncio loop for NiceGUI thread safety
                        if self._loop is not None:
                            self._loop.call_soon_threadsafe(callback, event)
                        else:
                            callback(event)
                    except Exception as e:
                        self._logger.error(f"Error en callback: {e}")
            except queue.Empty:
                continue

    def start(self) -> None:
        if not self._running:
            self._running = True
            # Capture the main event loop for thread-safe UI dispatch
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            self._processor = threading.Thread(
                target=self._process_events, daemon=True
            )
            self._processor.start()

    def stop(self) -> None:
        self._running = False


event_bus = EventBus()

# ── Lazy-initialized DB instance (deferred until NiceGUI context is ready) ──
_db_agent: Optional[DatabaseAgent] = None


def get_db_agent() -> DatabaseAgent:
    """Lazy initializer — avoids module-level instantiation outside async context."""
    global _db_agent
    if _db_agent is None:
        _db_agent = DatabaseAgent()
    return _db_agent


# ═══════════════════════════════════════════════════════════════════════════════
# ESTADO REACTIVO — VARIABLES NICEGUI (Vue-native)
# ═══════════════════════════════════════════════════════════════════════════════

class ReactiveState:
    """
    Centralized reactive state using NiceGUI variables.
    Updates are propagated by Vue automatically — no .refresh() calls.
    """

    def __init__(self):
        self.active_mods = ui.core.variable(0)
        self.pending_updates = ui.core.variable(0)
        self.conflicts_count = ui.core.variable(0)
        self.storage_used = ui.core.variable(0.0)
        self.selected_llm = ui.core.variable("DeepSeek")
        self.is_agent_connected = ui.core.variable(False)
        self.current_view = ui.core.variable("dashboard")
        self.is_loading = ui.core.variable(False)

    async def update_from_db(self) -> None:
        """Async pull from consolidated DB."""
        try:
            mods = await get_db_agent().get_mods(status='active')
            conflicts = await get_db_agent().get_conflicts(resolved=False)

            self.active_mods.set(len(mods))
            self.conflicts_count.set(len(conflicts))
            self.pending_updates.set(
                sum(1 for m in mods if m.get('needs_update', False))
            )
            total_size = sum(m.get('size_mb', 0) for m in mods)
            self.storage_used.set(round(total_size / 1024, 1))
        except Exception as e:
            logging.error(f"Error actualizando estado desde DB: {e}")

    def notify(self, message: str, type: str = "info") -> None:
        ui.notify(message, type=type)

    def handle_mod_added(self, event: SkyClawEvent) -> None:
        self.active_mods.set(self.active_mods.get() + 1)
        self.notify(f"Mod '{event.data.get('name')}' added!", type='positive')

    def handle_conflict_detected(self, event: SkyClawEvent) -> None:
        self.conflicts_count.set(self.conflicts_count.get() + 1)
        self.notify(
            f"Conflict: {event.data.get('description', 'Unknown')}",
            type='warning',
        )

    def on_connection_change(self, connected: bool) -> None:
        self.is_agent_connected.set(connected)


_state: Optional[ReactiveState] = None


def get_state() -> ReactiveState:
    """Lazy initializer — must be called within NiceGUI context (after ui.run)."""
    global _state
    if _state is None:
        _state = ReactiveState()
    return _state


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT COMMUNICATION — WS bridge (fire-and-forget)
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_daemon_message(data: Dict[str, Any]) -> None:
    """Translate daemon WS messages into EventBus events."""
    msg_type = data.get("type", "")

    if msg_type == "agent_result":
        action = data.get("action", "")
        if action == "install_complete":
            event_bus.publish(SkyClawEvent(
                type=EventType.MOD_ADDED,
                data=data.get("payload", {}),
                source="daemon",
            ))
        elif action == "conflict_found":
            event_bus.publish(SkyClawEvent(
                type=EventType.CONFLICT_DETECTED,
                data=data.get("payload", {}),
                source="daemon",
            ))

    elif msg_type == "response":
        event_bus.publish(SkyClawEvent(
            type=EventType.LLM_RESPONSE,
            data=data.get("payload", {}),
            source="daemon",
        ))

    elif msg_type == "broadcast":
        event_bus.publish(SkyClawEvent(
            type=EventType.EVENT_BROADCAST,
            data=data,
            source="daemon",
        ))


DAEMON_WS_URL = os.environ.get("SKY_CLAW_DAEMON_WS_URL", "ws://localhost:8765/ws/ui")

agent_client: Optional[AgentCommunicationClient] = None


def get_agent_client() -> AgentCommunicationClient:
    """Lazy initializer — deferred until NiceGUI context is ready."""
    global agent_client
    if agent_client is None:
        agent_client = AgentCommunicationClient(
            daemon_url=DAEMON_WS_URL,
            on_message=_handle_daemon_message,
            on_connection_change=get_state().on_connection_change,
        )
    return agent_client


# ═══════════════════════════════════════════════════════════════════════════════
# REUSABLE COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

def create_stat_card(
    title: str,
    value_var: Any,
    subtitle: str = "",
    icon_svg: str = "",
    trend: Optional[str] = None,
    trend_positive: bool = True,
) -> ui.element:
    trend_color = 'text-green-400' if trend_positive else 'text-red-400'

    with ui.element('div').classes(
        'bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6 sky-card-hover'
    ):
        with ui.row().classes('items-center justify-between mb-4'):
            if icon_svg:
                ui.html(f'''
                    <div class="w-12 h-12 rounded-xl flex items-center justify-center border"
                         style="background: linear-gradient(135deg, {COLORS['accent_violet']}20, {COLORS['accent_cyan']}20);
                                border-color: {COLORS['accent_violet']}30;">
                        {icon_svg}
                    </div>
                ''')
            ui.label(title).classes('text-[#9ca3af] text-sm')

        value_label = ui.label().classes('text-white text-4xl font-bold mb-2')
        value_label.bind_text_from(
            value_var,
            transform=lambda v: str(int(v) if isinstance(v, (int, float)) else v),
        )

        if subtitle or trend:
            with ui.row().classes('items-center justify-between'):
                if subtitle:
                    ui.label(subtitle).classes('text-[#6b7280] text-xs')
                if trend:
                    ui.label(trend).classes(f'text-xs font-semibold {trend_color}')


def create_feature_card(
    title: str,
    description: str,
    icon_svg: str,
    badge: Optional[str] = None,
    badge_type: str = 'info',
) -> ui.element:
    badge_class = f'sky-badge sky-badge--{badge_type}'

    with ui.element('div').classes(
        'bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6 sky-card-hover relative overflow-hidden'
    ):
        ui.html('<div class="sky-glow-overlay"></div>')
        with ui.column().classes('relative z-10'):
            with ui.row().classes('items-center gap-3 mb-4'):
                ui.html(f'''
                    <div class="w-14 h-14 rounded-2xl flex items-center justify-center border"
                         style="background: linear-gradient(135deg, {COLORS['accent_violet']}20, {COLORS['accent_violet']}05);
                                border-color: {COLORS['accent_violet']}30;">
                        {icon_svg}
                    </div>
                ''')
                with ui.column():
                    ui.label(title).classes('text-white font-bold text-lg')
                    if badge:
                        ui.label(badge).classes(badge_class)
            ui.label(description).classes('text-[#9ca3af] text-sm leading-relaxed')


def create_cta_button(
    text: str,
    on_click: Callable,
    variant: str = 'primary',
    icon_svg: Optional[str] = None,
) -> ui.button:
    if variant == 'primary':
        button_classes = (
            'sky-btn-cta px-8 py-4 rounded-full text-white font-semibold text-lg '
            'flex items-center gap-3 cursor-pointer'
        )
    elif variant == 'secondary':
        button_classes = (
            'sky-btn-secondary px-6 py-3 rounded-xl text-white font-medium '
            'flex items-center gap-2 cursor-pointer'
        )
    else:
        button_classes = (
            'px-4 py-2 text-[#9ca3af] hover:text-white hover:bg-[#1f2937] '
            'rounded-lg cursor-pointer transition-all duration-200'
        )

    button = ui.button().classes(button_classes).props('ripple')
    with button:
        if icon_svg:
            ui.html(f'<span class="mr-2">{icon_svg}</span>')
        ui.label(text)
        button.on('click', on_click)
    return button


def create_mod_list_item(
    name: str, status: str, size: str, on_click: Optional[Callable] = None
) -> ui.element:
    status_config = {
        'active': ('bg-green-500', 'Active'),
        'update': ('bg-yellow-500', 'Update'),
        'conflict': ('bg-red-500', 'Conflict'),
        'inactive': ('bg-gray-500', 'Inactive'),
    }
    status_color, status_label = status_config.get(status, ('bg-gray-500', status))

    with ui.element('div').classes(
        'flex items-center justify-between py-3 border-b border-[#1f2937] '
        'hover:bg-[#1f2937]/30 transition-colors cursor-pointer'
    ):
        if on_click:
            ui.element('div').classes('flex-1').on('click', on_click)

        with ui.row().classes('items-center gap-3 flex-1'):
            ui.html('''
                <div class="w-10 h-10 rounded-lg bg-[#1f2937] flex items-center justify-center">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                         stroke="#9ca3af" stroke-width="2">
                        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0
                                 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0
                                 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0
                                 21 16z"/>
                    </svg>
                </div>
            ''')
            with ui.column():
                ui.label(name).classes('text-white text-sm font-medium')
                ui.label(size).classes('text-[#6b7280] text-xs')

        ui.label(status_label).classes(
            f'{status_color} px-2 py-0.5 rounded-full text-xs font-medium text-white'
        )


def create_chat_message(
    message: str, is_user: bool = False, timestamp: str = ""
) -> ui.element:
    cls = (
        'sky-chat-message sky-chat-message--user'
        if is_user
        else 'sky-chat-message sky-chat-message--assistant'
    )
    with ui.element('div').classes(cls):
        ui.label(message).classes('text-[#e5e5e5] text-sm leading-relaxed')
        if timestamp:
            ui.label(timestamp).classes('text-[#6b7280] text-xs mt-1 block')


# ═══════════════════════════════════════════════════════════════════════════════
# LAYOUT SECTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def create_sidebar() -> None:
    with ui.element('div').classes(
        'w-64 h-screen bg-[#0a0a0a] border-r border-[#1f2937] flex flex-col sky-sidebar'
    ):
        # Logo
        with ui.element('div').classes('p-6 border-b border-[#1f2937]'):
            with ui.row().classes('items-center gap-3'):
                ui.html(f'''
                    <div class="w-10 h-10 rounded-xl flex items-center justify-center sky-glow-static"
                         style="background: linear-gradient(135deg,
                                {COLORS['accent_violet']}, {COLORS['accent_cyan']});">
                        <svg width="24" height="24" viewBox="0 0 24 24"
                             fill="none" stroke="white" stroke-width="2">
                            <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                            <path d="M2 17l10 5 10-5"/>
                            <path d="M2 12l10 5 10-5"/>
                        </svg>
                    </div>
                ''')
                with ui.column():
                    ui.label('Sky-Claw').classes('text-white font-bold text-lg')
                    # Connection indicator
                    with ui.row().classes('items-center gap-1'):
                        dot = ui.html('<span class="sky-connection-dot"></span>')
                        status_lbl = ui.label('Disconnected').classes(
                            'text-[#6b7280] text-xs'
                        )

        # Navigation
        with ui.element('div').classes('flex-1 p-4'):
            ui.label('NAVIGATION').classes(
                'text-[#6b7280] text-xs font-semibold tracking-wider mb-4 px-4'
            )
            nav_items = [
                ('Dashboard', True),
                ('Mods', False),
                ('Conflicts', False),
                ('Downloads', False),
                ('Settings', False),
            ]
            for text, active in nav_items:
                active_class = (
                    'bg-[#8b5cf6]/10 border-l-2 border-[#8b5cf6]' if active else ''
                )
                text_class = (
                    'text-white' if active else 'text-[#9ca3af] hover:text-white'
                )
                with ui.button().classes(
                    f'w-full flex items-center gap-3 px-4 py-3 rounded-xl '
                    f'text-left transition-all duration-200 hover:bg-[#1f2937] '
                    f'{active_class}'
                ).props('ripple flat').on(
                    'click', lambda t=text: get_state().notify(f'Navigate to {t}')
                ):
                    ui.label(text).classes(f'font-medium {text_class}')


def create_header() -> None:
    with ui.element('div').classes(
        'h-16 bg-[#0a0a0a] border-b border-[#1f2937] flex items-center '
        'justify-between px-6'
    ):
        with ui.column():
            ui.label('Dashboard').classes('text-white font-bold text-xl')
            ui.label('Welcome back, Dragonborn').classes('text-[#6b7280] text-xs')

        with ui.element('div').classes('flex-1 max-w-md mx-8'):
            with ui.element('div').classes(
                'relative bg-[#0f0f0f] border border-[#1f2937] rounded-xl '
                'overflow-hidden sky-input-premium'
            ):
                ui.input(
                    placeholder='Search mods, conflicts, or ask me anything...',
                    value='',
                ).classes(
                    'w-full px-4 py-3 bg-transparent border-none text-white '
                    'placeholder-[#6b7280] focus:outline-none'
                )

        with ui.row().classes('items-center gap-4'):
            with ui.element('div').classes(
                'w-10 h-10 rounded-full flex items-center justify-center '
                'text-white font-bold cursor-pointer sky-card-hover'
            ).style(
                f'background: linear-gradient(135deg, '
                f'{COLORS["accent_violet"]}, {COLORS["accent_pink"]});'
            ):
                ui.label('DS')


def create_stats_section() -> None:
    with ui.element('div').classes('grid grid-cols-4 gap-6 mb-8'):
        create_stat_card(
            title='Active Mods',
            value_var=get_state().active_mods,
            subtitle='from last week',
            icon_svg=f'''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="{COLORS['accent_violet']}" stroke-width="2">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2
                             0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2
                             2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                </svg>
            ''',
            trend='↑ 12%',
            trend_positive=True,
        )
        create_stat_card(
            title='Pending',
            value_var=get_state().pending_updates,
            subtitle='Updates available',
            icon_svg='''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="#eab308" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <polyline points="12 6 12 12 16 14"/>
                </svg>
            ''',
        )
        create_stat_card(
            title='Conflicts',
            value_var=get_state().conflicts_count,
            subtitle='Needs attention',
            icon_svg='''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="#ef4444" stroke-width="2">
                    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2
                             2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
                    <line x1="12" y1="9" x2="12" y2="13"/>
                    <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
            ''',
        )
        create_stat_card(
            title='Storage',
            value_var=get_state().storage_used,
            subtitle='GB used',
            icon_svg=f'''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="{COLORS['accent_cyan']}" stroke-width="2">
                    <rect x="2" y="4" width="20" height="16" rx="2"/>
                    <path d="M6 8h.01"/>
                    <path d="M10 8h.01"/>
                    <path d="M14 8h.01"/>
                </svg>
            ''',
        )


def create_features_section() -> None:
    with ui.element('div').classes('mb-8'):
        ui.label('Core Features').classes('text-white text-xl font-bold mb-6')
        with ui.element('div').classes('grid grid-cols-3 gap-6'):
            create_feature_card(
                title='Smart Search',
                description='Natural language search across thousands of '
                'Skyrim mods with AI-powered recommendations',
                icon_svg=f'''
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="{COLORS['accent_violet']}" stroke-width="2">
                        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
                        <path d="M11 8v6"/><path d="M8 11h6"/>
                    </svg>
                ''',
                badge='NEW',
                badge_type='violet',
            )
            create_feature_card(
                title='Conflict Resolution',
                description='Automatically detect and resolve mod conflicts '
                'using advanced dependency analysis',
                icon_svg='''
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="#ef4444" stroke-width="2">
                        <path d="M12 9v4"/><path d="M12 17h.01"/>
                        <path d="M3.44 18.54l7.04-12.15a2 2 0 0 1 3.04
                                 0l7.04 12.15a2 2 0 0 1-1.52 2.93H4.96a2
                                 2 0 0 1-1.52-2.93z"/>
                    </svg>
                ''',
            )
            create_feature_card(
                title='Zero-Trust Security',
                description='HITL protection with Telegram integration for '
                'approving external downloads safely',
                icon_svg=f'''
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="{COLORS['accent_cyan']}" stroke-width="2">
                        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                        <path d="m9 12 2 2 4-4"/>
                    </svg>
                ''',
            )


def create_mods_preview() -> None:
    with ui.element('div').classes(
        'bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6'
    ):
        with ui.row().classes('items-center justify-between mb-6'):
            ui.label('Recent Mods').classes('text-white font-bold text-lg')
            create_cta_button(
                text='View All',
                on_click=lambda: get_state().notify('Navigating to Mods...'),
                variant='secondary',
            ).classes('px-4 py-2 rounded-lg text-sm')

        # Fallback sample data when DB is empty
        sample_mods = [
            {'name': 'Skyrim 202X', 'status': 'active', 'size_mb': 2400},
            {'name': 'Immersive Armors', 'status': 'active', 'size_mb': 156},
            {'name': 'Lux Via', 'status': 'update', 'size_mb': 89},
            {'name': 'Ordinator', 'status': 'conflict', 'size_mb': 45},
        ]

        for mod in sample_mods:
            size = (
                f"{mod.get('size_mb', 0) / 1024:.1f} GB"
                if mod.get('size_mb', 0) > 1024
                else f"{mod.get('size_mb', 0)} MB"
            )
            create_mod_list_item(
                name=mod['name'],
                status=mod.get('status', 'inactive'),
                size=size,
                on_click=lambda m=mod: get_state().notify(f'Viewing {m["name"]}'),
            )


def create_chat_preview() -> None:
    with ui.element('div').classes(
        'bg-[#0f0f0f] border border-[#1f2937] rounded-2xl overflow-hidden'
    ):
        # Header
        with ui.element('div').classes(
            'p-4 border-b border-[#1f2937]'
        ).style(
            f'background: linear-gradient(135deg, '
            f'{COLORS["accent_violet"]}20, {COLORS["accent_cyan"]}20);'
        ):
            with ui.row().classes('items-center gap-3'):
                ui.html(f'''
                    <div class="w-10 h-10 rounded-xl flex items-center
                         justify-center sky-glow-static"
                         style="background: linear-gradient(135deg,
                                {COLORS['accent_violet']},
                                {COLORS['accent_cyan']});">
                        <svg width="20" height="20" viewBox="0 0 24 24"
                             fill="none" stroke="white" stroke-width="2">
                            <path d="M12 2a10 10 0 0 1 10 10c0 5.52-4.48
                                     10-10 10S2 17.52 2 12 6.48 2 12 2z"/>
                            <path d="M12 8v4"/>
                            <path d="M12 16h.01"/>
                        </svg>
                    </div>
                ''')
                with ui.column():
                    ui.label('AI Assistant').classes('text-white font-bold')
                    ui.label('Powered by DeepSeek').classes(
                        'text-[#6b7280] text-xs'
                    )

        # Messages
        messages_container = ui.element('div').classes(
            'p-4 h-48 overflow-y-auto sky-scrollbar'
        )
        with messages_container:
            create_chat_message(
                "Hello, Dragonborn! I can help you manage your Skyrim mods. "
                "What would you like to do?",
                is_user=False,
                timestamp='Now',
            )

        # Input
        with ui.element('div').classes('p-4 border-t border-[#1f2937]'):
            with ui.element('div').classes('flex gap-2'):
                chat_input = ui.input(
                    placeholder='Ask me anything about your mods...',
                    value='',
                ).classes(
                    'flex-1 bg-[#0a0a0a] border border-[#1f2937] rounded-xl '
                    'px-4 py-3 text-white placeholder-[#6b7280] sky-input-premium'
                )

                def send_message():
                    msg = chat_input.value.strip()
                    if msg:
                        with messages_container:
                            create_chat_message(
                                msg, is_user=True, timestamp='Just now'
                            )
                        chat_input.value = ''
                        get_state().notify('Processing your request...', type='info')
                        # Fire-and-forget: send to daemon, NOT local LLM
                        asyncio.create_task(process_user_message(msg))

                ui.button().classes(
                    'p-3 rounded-xl transition-colors sky-btn-cta'
                ).props('ripple').on('click', send_message)

                ui.html('''
                    <svg width="20" height="20" viewBox="0 0 24 24"
                         fill="none" stroke="white" stroke-width="2">
                        <line x1="22" y1="2" x2="11" y2="13"/>
                        <polygon points="22 2 15 22 11 13 2 9 22 2"/>
                    </svg>
                ''')


async def process_user_message(message: str) -> None:
    """
    Fire-and-forget: sends the user message to the Background Daemon
    via WebSocket instead of running LLM inference locally.
    """
    try:
        sent = await get_agent_client().send_chat_message(message)
        if not sent:
            # Fallback: if daemon is disconnected, publish locally
            event_bus.publish(SkyClawEvent(
                type=EventType.LLM_RESPONSE,
                data={'response': '⚠️ Daemon offline — message queued.'},
                source='ui_fallback',
            ))
    except Exception as e:
        logging.error(f"Error enviando mensaje al daemon: {e}")


def create_cta_section() -> None:
    with ui.element('div').classes(
        'relative rounded-3xl p-12 overflow-hidden'
    ).style(
        f'background: linear-gradient(135deg, {COLORS["accent_violet"]}10, '
        f'#0f0f0f, {COLORS["accent_cyan"]}10);'
        f'border: 1px solid {COLORS["accent_violet"]}30;'
    ):
        ui.html('''
            <div style="position:absolute;top:0;left:25%;width:384px;
                 height:384px;background:rgba(139,92,246,0.2);
                 border-radius:50%;filter:blur(128px);
                 pointer-events:none;"></div>
            <div style="position:absolute;bottom:0;right:25%;width:384px;
                 height:384px;background:rgba(6,182,212,0.1);
                 border-radius:50%;filter:blur(128px);
                 pointer-events:none;"></div>
        ''')

        with ui.column().classes('relative z-10 items-center text-center'):
            ui.label('VERSION 2.0').classes(
                'px-4 py-1 rounded-full text-sm font-semibold mb-6 '
                'sky-badge sky-badge--violet'
            )
            ui.label(
                'Ready to transform your modding experience?'
            ).classes('text-white text-5xl font-bold leading-tight mb-6')
            ui.label(
                'Sky-Claw uses advanced AI to search, install, and manage '
                'your Skyrim mods. Say goodbye to conflicts and hello to a '
                'perfectly optimized load order.'
            ).classes('text-[#9ca3af] text-lg max-w-2xl mb-10 leading-relaxed')

            with ui.row().classes('items-center gap-4'):
                create_cta_button(
                    text='Get Started',
                    on_click=lambda: get_state().notify(
                        'Sky-Claw launching...', type='positive'
                    ),
                    variant='primary',
                    icon_svg='''
                        <svg width="24" height="24" viewBox="0 0 24 24"
                             fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>
                        </svg>
                    ''',
                )
                create_cta_button(
                    text='Watch Demo',
                    on_click=lambda: get_state().notify('Opening demo...'),
                    variant='secondary',
                    icon_svg='''
                        <svg width="20" height="20" viewBox="0 0 24 24"
                             fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="5 3 19 12 5 21 5 3"/>
                        </svg>
                    ''',
                )


# ═══════════════════════════════════════════════════════════════════════════════
# EVENT HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def handle_mod_added(event: SkyClawEvent) -> None:
    get_state().handle_mod_added(event)


def handle_conflict_detected(event: SkyClawEvent) -> None:
    get_state().handle_conflict_detected(event)


def handle_llm_response(event: SkyClawEvent) -> None:
    response = event.data.get('response', event.data.get('text', ''))
    get_state().notify(f"AI: {response[:80]}...", type='info')


event_bus.subscribe(EventType.MOD_ADDED, handle_mod_added)
event_bus.subscribe(EventType.CONFLICT_DETECTED, handle_conflict_detected)
event_bus.subscribe(EventType.LLM_RESPONSE, handle_llm_response)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PAGE
# ═══════════════════════════════════════════════════════════════════════════════

@ui.page('/')
def main_page():
    ui.dark_mode().enable()

    # Serve extracted CSS
    css_href = '/static/sky_claw_styles.css'
    ui.add_head_html(f'''
        <link rel="stylesheet" href="{css_href}">
    ''')

    with ui.element('div').classes('flex min-h-screen bg-[#000000]'):
        create_sidebar()
        with ui.element('div').classes('flex-1 flex flex-col sky-main-content'):
            create_header()
            with ui.element('div').classes(
                'flex-1 p-8 overflow-y-auto sky-scrollbar'
            ).style(
                f'background: radial-gradient(ellipse at top, '
                f'{COLORS["glow_violet"]}12, transparent 50%), '
                f'radial-gradient(ellipse at bottom right, '
                f'{COLORS["glow_cyan"]}8, transparent 50%);'
            ):
                create_stats_section()
                create_features_section()
                with ui.element('div').classes('grid grid-cols-2 gap-8 mb-8'):
                    create_mods_preview()
                    create_chat_preview()
                create_cta_section()


# ═══════════════════════════════════════════════════════════════════════════════
# APP SETUP & ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def setup_app():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    # Serve CSS from the project's frontend/css directory
    app.add_static_files('/static', str(_CSS_PATH.parent))

    # Initialize DB and seed sample data
    async def _init():
        await get_db_agent().init_db()
        mods = await get_db_agent().get_mods()
        if not mods:
            await get_db_agent().add_mod('Skyrim 202X', '9.0', 2400, 'Nexusmods')
            await get_db_agent().add_mod('Immersive Armors', '8.1', 156, 'Nexusmods')
            await get_db_agent().add_mod('Lux Via', '1.5', 89, 'Nexusmods')
        await get_state().update_from_db()

    app.on_startup(lambda: asyncio.create_task(_init()))

    # Start event bus and agent communication client
    event_bus.start()
    get_agent_client().start()

    # Cleanup
    async def _cleanup():
        event_bus.stop()
        await get_agent_client().stop()

    app.on_shutdown(lambda: asyncio.create_task(_cleanup()))


def cleanup():
    event_bus.stop()


if __name__ in {'__main__', '__mp_main__'}:
    setup_app()

    try:
        ui.run(
            title='Sky-Claw — Mod Manager v2.0',
            port=8080,
            host=os.environ.get('SKY_CLAW_BIND_HOST', '127.0.0.1'),
            reload=True,
            show=True,
            dark=True,
            favicon='🔮',
        )
    except KeyboardInterrupt:
        cleanup()
