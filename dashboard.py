import os
import sqlite3
import json
import threading
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

# Import bot components so dashboard can host both bot & web UI together!
try:
    from bot import client, DISCORD_TOKEN
    BOT_IMPORT_SUCCESS = True
except Exception as e:
    print("Warning: Could not import bot:", e)
    BOT_IMPORT_SUCCESS = False

app = FastAPI(title="Seraphine Bot & Dashboard")

DB_NAME = "bot_memory.db"
CONFIG_FILE = "config.json"

@app.on_event("startup")
async def startup_event():
    if BOT_IMPORT_SUCCESS and DISCORD_TOKEN:
        def run_bot():
            try:
                print("Starting Discord Bot in background thread...")
                client.run(DISCORD_TOKEN)
            except Exception as e:
                print(f"Bot error: {e}")
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()

def get_default_config():
    return {
        "ai_chat_enabled": True,
        "music_enabled": True,
        "automod_enabled": True,
        "voice_log_enabled": True
    }

def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = get_default_config()
        save_config(cfg)
        return cfg
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return get_default_config()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

def get_db_stats():
    stats = {"conversations": 0, "infractions": 0}
    if not os.path.exists(DB_NAME):
        return stats
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM conversation")
        stats["conversations"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM infractions")
        stats["infractions"] = c.fetchone()[0]
        conn.close()
    except Exception as e:
        print("DB error:", e)
    return stats

def get_recent_infractions(limit=10):
    if not os.path.exists(DB_NAME):
        return []
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM infractions ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows
    except:
        return []

def get_recent_logs(limit=50):
    log_path = "bot.log"
    if not os.path.exists(log_path):
        return ["No bot.log found yet."]
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [line.strip() for line in lines[-limit:][::-1]]
    except Exception as e:
        return [f"Error reading logs: {e}"]

def get_music_queues():
    """Live music queue snapshot from the shared bot module (same process)."""
    if not BOT_IMPORT_SUCCESS:
        return []
    try:
        import bot as _bot
        out = []
        for gid, queue in _bot.music_queues.items():
            if queue:
                out.append({
                    "guild_id": gid,
                    "loop": bool(_bot.music_loop.get(gid)),
                    "tracks": [{"title": getattr(p, "title", "?"), "url": getattr(p, "url", "")} for p in queue[:10]],
                })
        return out
    except Exception as e:
        print("Music queue read error:", e)
        return []

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Seraphine Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        darkbg: '#0f172a',
                        cardbg: '#1e293b',
                        accent: '#6366f1'
                    }
                }
            }
        }
    </script>
</head>
<body class="bg-darkbg text-slate-100 min-h-screen font-sans antialiased">
    <div class="flex h-screen overflow-hidden">
        <!-- Sidebar -->
        <div class="w-64 bg-cardbg border-r border-slate-800 flex flex-col justify-between hidden md:flex">
            <div class="p-6">
                <div class="flex items-center space-x-3 mb-8">
                    <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center font-bold text-xl shadow-lg shadow-indigo-500/30">S</div>
                    <div>
                        <h1 class="font-bold text-lg leading-tight">Seraphine</h1>
                        <p class="text-xs text-slate-400">Control Panel</p>
                    </div>
                </div>
                <nav class="space-y-1">
                    <a href="#overview" class="flex items-center px-4 py-3 text-sm font-medium rounded-lg bg-indigo-600 text-white shadow-md">📊 Overview</a>
                    <a href="#features" class="flex items-center px-4 py-3 text-sm font-medium rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white transition">⚙️ Feature Toggles</a>
                    <a href="#infractions" class="flex items-center px-4 py-3 text-sm font-medium rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white transition">🛡️ Mod Infractions</a>
                    <a href="#logs" class="flex items-center px-4 py-3 text-sm font-medium rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white transition">📜 Live Logs</a>
                </nav>
            </div>
            <div class="p-6 border-t border-slate-800 text-xs text-slate-400">
                <p>Status: <span class="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1 animate-pulse"></span> Bot & Web Active</p>
            </div>
        </div>

        <!-- Main Content -->
        <div class="flex-1 flex flex-col overflow-y-auto">
            <header class="bg-cardbg border-b border-slate-800 p-6 flex justify-between items-center sticky top-0 z-10 shadow-sm">
                <div>
                    <h2 class="text-xl font-bold">Seraphine Bot Dashboard</h2>
                    <p class="text-xs text-slate-400">Manage your Discord bot features, settings, and logs in real-time.</p>
                </div>
                <div class="flex items-center space-x-3">
                    <span class="px-3 py-1 rounded-full text-xs bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 font-medium">OpenRouter AI</span>
                </div>
            </header>

            <main class="p-6 space-y-8 max-w-7xl mx-auto w-full">
                <!-- Overview Stats -->
                <section id="overview" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
                    <div class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total Conversations</p>
                        <h3 class="text-3xl font-extrabold mt-2 text-indigo-400">{{ stats.conversations }}</h3>
                    </div>
                    <div class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Mod Infractions</p>
                        <h3 class="text-3xl font-extrabold mt-2 text-rose-400">{{ stats.infractions }}</h3>
                    </div>
                    <div class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Bot & DB Status</p>
                        <h3 class="text-3xl font-extrabold mt-2 text-emerald-400">Online 24/7</h3>
                    </div>
                    <div class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Dashboard Version</p>
                        <h3 class="text-3xl font-extrabold mt-2 text-amber-400">v1.0</h3>
                    </div>
                </section>

                <!-- Feature Toggles -->
                <section id="features" class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                    <h3 class="text-lg font-bold mb-4 flex items-center">⚙️ Feature Control Panel</h3>
                    <form action="/update-config" method="POST" class="grid grid-cols-1 md:grid-cols-2 gap-6">
                        <div class="flex items-center justify-between p-4 bg-slate-900/50 rounded-xl border border-slate-800">
                            <div>
                                <h4 class="font-semibold text-sm">AI Chat (OpenRouter)</h4>
                                <p class="text-xs text-slate-400">Enable AI response and chat handling</p>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="ai_chat_enabled" value="true" {% if config.ai_chat_enabled %}checked{% endif %} class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-indigo-600"></div>
                            </label>
                        </div>

                        <div class="flex items-center justify-between p-4 bg-slate-900/50 rounded-xl border border-slate-800">
                            <div>
                                <h4 class="font-semibold text-sm">Music Player System</h4>
                                <p class="text-xs text-slate-400">Enable /splay, /squeue, and audio queues</p>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="music_enabled" value="true" {% if config.music_enabled %}checked{% endif %} class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-indigo-600"></div>
                            </label>
                        </div>

                        <div class="flex items-center justify-between p-4 bg-slate-900/50 rounded-xl border border-slate-800">
                            <div>
                                <h4 class="font-semibold text-sm">Auto-Moderation / Toxic Filter</h4>
                                <p class="text-xs text-slate-400">Auto-delete toxic messages & log infractions</p>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="automod_enabled" value="true" {% if config.automod_enabled %}checked{% endif %} class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-indigo-600"></div>
                            </label>
                        </div>

                        <div class="flex items-center justify-between p-4 bg-slate-900/50 rounded-xl border border-slate-800">
                            <div>
                                <h4 class="font-semibold text-sm">Voice State & Move Logs</h4>
                                <p class="text-xs text-slate-400">Log voice channel movements & audit logs</p>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="voice_log_enabled" value="true" {% if config.voice_log_enabled %}checked{% endif %} class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-indigo-600"></div>
                            </label>
                        </div>

                        <div class="md:col-span-2 flex justify-end">
                            <button type="submit" class="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-medium text-sm rounded-xl shadow-lg shadow-indigo-600/30 transition">Save Changes</button>
                        </div>
                    </form>
                </section>

                <!-- Infractions Table -->
                <section id="infractions" class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                    <h3 class="text-lg font-bold mb-4 flex items-center">🛡️ Recent Moderation Infractions</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm text-slate-300">
                            <thead class="bg-slate-900/80 text-xs uppercase text-slate-400">
                                <tr>
                                    <th class="p-3">ID</th>
                                    <th class="p-3">Guild ID</th>
                                    <th class="p-3">User ID</th>
                                    <th class="p-3">Action</th>
                                    <th class="p-3">Reason</th>
                                    <th class="p-3">Timestamp</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-slate-800">
                                {% for inf in infractions %}
                                <tr class="hover:bg-slate-800/50">
                                    <td class="p-3 font-mono text-xs">{{ inf.id }}</td>
                                    <td class="p-3 font-mono text-xs">{{ inf.guild_id }}</td>
                                    <td class="p-3 font-mono text-xs">{{ inf.user_id }}</td>
                                    <td class="p-3"><span class="px-2 py-1 rounded text-xs bg-rose-500/10 text-rose-400 border border-rose-500/20 font-semibold">{{ inf.action_type }}</span></td>
                                    <td class="p-3">{{ inf.reason }}</td>
                                    <td class="p-3 text-xs text-slate-400">{{ inf.timestamp }}</td>
                                </tr>
                                {% else %}
                                <tr>
                                    <td colspan="6" class="p-6 text-center text-slate-500">No infractions recorded yet. Good server!</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </section>

                <!-- Music Queues -->
                <section id="music" class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                    <h3 class="text-lg font-bold mb-4 flex items-center">🎵 Live Music Queues</h3>
                    <div class="space-y-4">
                        {% for q in music_queues %}
                        <div class="bg-slate-900/50 rounded-xl border border-slate-800 p-4">
                            <div class="flex items-center justify-between mb-2">
                                <span class="font-mono text-xs text-slate-400">Guild {{ q.guild_id }}</span>
                                <span class="px-2 py-1 rounded text-xs {% if q.loop %}bg-indigo-500/10 text-indigo-400 border border-indigo-500/20{% else %}bg-slate-700/30 text-slate-400 border border-slate-600/30{% endif %} font-semibold">
                                    {% if q.loop %}🔁 Loop ON{% else %}Loop OFF{% endif %}
                                </span>
                            </div>
                            <ol class="text-sm text-slate-300 space-y-1 list-decimal list-inside">
                                {% for t in q.tracks %}
                                <li>{% if t.url %}<a href="{{ t.url }}" class="hover:text-indigo-400 transition" target="_blank">{{ t.title }}</a>{% else %}{{ t.title }}{% endif %}</li>
                                {% endfor %}
                            </ol>
                        </div>
                        {% else %}
                        <p class="text-slate-500 text-sm">No active music queues right now.</p>
                        {% endfor %}
                    </div>
                </section>

                <!-- Live Logs -->
                <section id="logs" class="bg-cardbg p-6 rounded-2xl border border-slate-800 shadow-xl">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-lg font-bold flex items-center">📜 Live Bot Logs (bot.log)</h3>
                        <a href="/" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-xs rounded-lg text-slate-300 transition">🔄 Refresh Logs</a>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-slate-300 h-64 overflow-y-auto border border-slate-800/80 space-y-1">
                        {% for log in logs %}
                        <div>{{ log }}</div>
                        {% endfor %}
                    </div>
                </section>
            </main>
        </div>
    </div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    config = load_config()
    stats = get_db_stats()
    infractions = get_recent_infractions()
    logs = get_recent_logs()
    music_queues = get_music_queues()
    
    return HTMLResponse(content=render_html(config, stats, infractions, logs, music_queues))

def render_html(config, stats, infractions, logs, music_queues=None):
    try:
        from jinja2 import Template
        t = Template(HTML_TEMPLATE)
        return t.render(config=config, stats=stats, infractions=infractions, logs=logs, music_queues=music_queues or [])
    except Exception as e:
        return HTML_TEMPLATE.replace("{{ stats.conversations }}", str(stats["conversations"])).replace("{{ stats.infractions }}", str(stats["infractions"]))

@app.post("/update-config")
async def update_config(
    ai_chat_enabled: bool = Form(False),
    music_enabled: bool = Form(False),
    automod_enabled: bool = Form(False),
    voice_log_enabled: bool = Form(False)
):
    cfg = {
        "ai_chat_enabled": ai_chat_enabled,
        "music_enabled": music_enabled,
        "automod_enabled": automod_enabled,
        "voice_log_enabled": voice_log_enabled
    }
    save_config(cfg)
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", 8000)))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port)
