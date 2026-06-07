import requests
import folium
import time
import itertools
import math
import webbrowser
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# ========== 配置 ==========
AMAP_KEY = "f02dcb496107b3aad522d7e0a3934997"
MARKERS_FILE = "markers.json"
COORD_MEMORY_FILE = "coord_memory.json"
ALIAS_FILE = "shop_alias.json"
ROUTE_MEMORY_FILE = "route_memory.json"
REMINDER_FILE = "reminders.json"
BLOCKAGE_FILE = "blockages.json"
DETOUR_FILE = "detours.json"
FLOOR_MAP_FILE = "floor_maps.json"
NIGHT_MODE_FILE = "night_mode.json"
MAP_PORT = 5000

# ========== JSON 工具 ==========
def load_json(path):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_markers():
    return load_json(MARKERS_FILE)

def save_markers(markers):
    save_json(MARKERS_FILE, markers)

def resolve_alias(name, aliases):
    if name in aliases:
        return name, aliases[name]
    for main_name, info in aliases.items():
        if name in info.get("aliases", []):
            return main_name, info
    return None, None

def make_route_key(addr1, addr2):
    return f"{addr1} -> {addr2}"

def address_to_coord(address, night_mode=False):
    aliases = load_json(ALIAS_FILE)
    coord_memory = load_json(COORD_MEMORY_FILE)
    markers = load_markers()
    night_config = load_json(NIGHT_MODE_FILE)
    
    if night_mode:
        for mall, info in night_config.items():
            if address == mall or address in aliases.get(mall, {}).get("aliases", []):
                return info.get("night_entrance")
    
    for m in markers:
        if m["name"] == address:
            return f"{m['lng']},{m['lat']}"
    
    main_name, alias_info = resolve_alias(address, aliases)
    if alias_info and "coord" in alias_info:
        return alias_info["coord"]
    
    if address in coord_memory:
        return coord_memory[address]["correct_coord"]
    
    try:
        r = requests.get("https://restapi.amap.com/v3/geocode/geo",
                         params={"key": AMAP_KEY, "address": address}, timeout=5).json()
        if r.get("status") == "1" and r.get("geocodes"):
            return r["geocodes"][0]["location"]
    except:
        pass
    return None

def get_distance(origin, destination):
    try:
        r = requests.get("https://restapi.amap.com/v4/direction/bicycling",
                         params={"key": AMAP_KEY, "origin": origin, "destination": destination}, timeout=5).json()
        if r.get("errcode") == 0 and r["data"]["paths"]:
            return int(r["data"]["paths"][0]["distance"])
    except:
        pass
    return 999999

def find_best_order(coords_dict):
    names = list(coords_dict.keys())
    if len(names) <= 1:
        return names, 0
    best_order = names[:]
    best_distance = float('inf')
    first = names[0]
    others = names[1:]
    for perm in itertools.permutations(others):
        order = [first] + list(perm)
        total = sum(get_distance(coords_dict[order[i]], coords_dict[order[i+1]]) for i in range(len(order)-1))
        if total < best_distance:
            best_distance = total
            best_order = order
    return best_order, best_distance

def get_route_coords(origin, destination):
    try:
        r = requests.get("https://restapi.amap.com/v4/direction/bicycling",
                         params={"key": AMAP_KEY, "origin": origin, "destination": destination}, timeout=10).json()
        if r.get("errcode") == 0 and r["data"]["paths"]:
            coords = []
            for step in r["data"]["paths"][0]["steps"]:
                for pt in step["polyline"].split(";"):
                    lon, lat = pt.split(",")
                    coords.append((float(lat), float(lon)))
            return coords
    except:
        pass
    return []

def get_route_instructions(origin, destination):
    try:
        r = requests.get("https://restapi.amap.com/v4/direction/bicycling",
                         params={"key": AMAP_KEY, "origin": origin, "destination": destination}, timeout=10).json()
        if r.get("errcode") == 0 and r["data"]["paths"]:
            return [s["instruction"] for s in r["data"]["paths"][0]["steps"]]
    except:
        pass
    return []

def get_multi_route(ordered_addresses, coords_dict, route_memory, detours, night_mode=False):
    all_coords = []
    all_instructions = []
    for i in range(len(ordered_addresses) - 1):
        addr_from = ordered_addresses[i]
        addr_to = ordered_addresses[i+1]
        suffix = "_night" if night_mode else ""
        rk = make_route_key(addr_from, addr_to) + suffix
        nk = make_route_key(addr_from, addr_to)
        
        if rk in route_memory:
            seg = [(lat, lon) for lat, lon in route_memory[rk]["coords"]]
            all_coords = all_coords + seg if i == 0 else all_coords + seg[1:]
            all_instructions.append(f"[记忆路线] {addr_from} → {addr_to}")
            continue
        if not night_mode and nk in route_memory:
            seg = [(lat, lon) for lat, lon in route_memory[nk]["coords"]]
            all_coords = all_coords + seg if i == 0 else all_coords + seg[1:]
            all_instructions.append(f"[记忆路线] {addr_from} → {addr_to}")
            continue
        
        if nk in detours:
            waypoints = detours[nk]["waypoints"]
            seg = get_route_coords(coords_dict[addr_from], waypoints[0])
            for j in range(len(waypoints)-1):
                s = get_route_coords(waypoints[j], waypoints[j+1])
                if s: seg += s[1:]
            final = get_route_coords(waypoints[-1], coords_dict[addr_to])
            if final: seg += final[1:]
            all_coords = all_coords + seg if i == 0 else all_coords + seg[1:]
            all_instructions.append(f"[绕行路线] {addr_from} → {addr_to}")
            continue
        
        seg = get_route_coords(coords_dict[addr_from], coords_dict[addr_to])
        instructions = get_route_instructions(coords_dict[addr_from], coords_dict[addr_to])
        if seg:
            all_coords = all_coords + seg if i == 0 else all_coords + seg[1:]
            all_instructions.extend(instructions)
    return all_coords, all_instructions

def draw_map_html(coords, addresses, ordered_addresses, night_mode=False, blockages=None, detours=None):
    if not coords: return None
    m = folium.Map(location=coords[0], zoom_start=14, tiles=None)
    folium.TileLayer(
        'http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
        attr='高德', overlay=False, control=True
    ).add_to(m)
    colors = ["orange","purple","darkblue","darkred","cadetblue","darkgreen","pink","black"]
    for idx, addr in enumerate(ordered_addresses):
        c = addresses[addr].split(",")
        coord = (float(c[1]), float(c[0]))
        if idx == 0: icon = folium.Icon(color="green", icon="play")
        elif idx == len(ordered_addresses)-1: icon = folium.Icon(color="red", icon="stop")
        else: icon = folium.Icon(color=colors[idx%len(colors)], icon="flag")
        folium.Marker(coord, popup=addr, icon=icon).add_to(m)
    if blockages:
        for info in blockages.values():
            lon, lat = info["coord"].split(",")
            folium.Marker((float(lat),float(lon)), popup="🚧 "+info.get("note",""), icon=folium.Icon(color="red",icon="remove-sign")).add_to(m)
    if detours:
        for info in detours.values():
            for wp in info["waypoints"]:
                lon, lat = wp.split(",")
                folium.Marker((float(lat),float(lon)), popup="绕行点", icon=folium.Icon(color="orange",icon="arrow-right")).add_to(m)
    line_color = "#FFD700" if night_mode else "blue"
    folium.PolyLine(coords, color=line_color, weight=5, opacity=0.8).add_to(m)
    path = "route_map.html"
    m.save(path)
    return os.path.abspath(path)

# ========== HTTP 服务 ==========
class MarkerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/markers':
            self.send_json(load_markers())
        elif self.path == '/map':
            with open('interactive_map.html','r',encoding='utf-8') as f:
                self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers()
                self.wfile.write(f.read().encode())
    
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        markers = load_markers()
        if self.path == '/add_marker':
            markers.append({"name":body["name"],"lat":body["lat"],"lng":body["lng"]})
        elif self.path == '/update_marker':
            for m in markers:
                if m["name"] == body["old_name"]:
                    m.update({"name":body.get("new_name",m["name"]),"lat":body["lat"],"lng":body["lng"]})
        elif self.path == '/delete_marker':
            markers = [m for m in markers if m["name"]!=body["name"]]
        save_markers(markers); self.send_json({"status":"ok"})
    
    def send_json(self, data):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps(data).encode())

def start_server():
    HTTPServer(('0.0.0.0', MAP_PORT), MarkerHandler).serve_forever()

# ========== 交互地图 HTML ==========
INTERACTIVE_MAP_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"><title>交互地图</title>
<style>html,body,#map{height:100%;margin:0;padding:0;}
.toolbar{position:absolute;top:10px;left:50%;transform:translateX(-50%);z-index:999;background:white;padding:6px 12px;border-radius:20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);font-size:13px;}
.toolbar button{margin:0 3px;padding:5px 10px;border-radius:12px;border:none;cursor:pointer;}
.btn-add{background:#4CAF50;color:white;}.btn-save{background:#2196F3;color:white;}.btn-edit{background:#FF9800;color:white;}
.info{position:absolute;bottom:20px;left:10px;z-index:999;background:white;padding:6px 12px;border-radius:10px;box-shadow:0 2px 6px rgba(0,0,0,0.3);font-size:12px;}</style>
</head><body>
<div id="map"></div>
<div class="toolbar"><span id="modeLabel">浏览</span>
<button class="btn-add" onclick="setMode('add')">+添加</button>
<button class="btn-edit" onclick="setMode('edit')">编辑</button>
<button class="btn-save" onclick="saveMarkers()">保存</button></div>
<div class="info" id="info">点击添加|拖动调整|右键删除</div>
<script>var AMAP_KEY="f02dcb496107b3aad522d7e0a3934997";</script>
<script src="https://webapi.amap.com/maps?v=1.4.15&key=f02dcb496107b3aad522d7e0a3934997"></script>
<script>
var map=new AMap.Map('map',{zoom:14,center:[116.397428,39.90923]});
var markers=[],currentMode='browse',allMarkers=[];
fetch('/markers').then(r=>r.json()).then(d=>{allMarkers=d;renderMarkers();});
function renderMarkers(){
markers.forEach(m=>map.remove(m));markers=[];
allMarkers.forEach((m,idx)=>{
var mk=new AMap.Marker({position:[m.lng,m.lat],title:m.name,label:{content:m.name,offset:new AMap.Pixel(0,-25)},draggable:currentMode==='edit'});
mk.on('dragend',e=>{m.lat=e.lnglat.getLat();m.lng=e.lnglat.getLng();document.getElementById('info').innerText='已移动:'+m.name;});
mk.on('rightclick',()=>{if(confirm('删除:'+m.name+'?')){fetch('/delete_marker',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:m.name})}).then(()=>{allMarkers.splice(idx,1);renderMarkers();});}});
mk.setMap(map);markers.push(mk);
});}
function setMode(m){currentMode=m;document.getElementById('modeLabel').innerText=m==='add'?'添加':m==='edit'?'编辑':'浏览';renderMarkers();}
map.on('click',e=>{if(currentMode==='add'){var n=prompt('标记名:');if(n){fetch('/add_marker',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,lat:e.lnglat.getLat(),lng:e.lnglat.getLng()})}).then(()=>{allMarkers.push({name:n,lat:e.lnglat.getLat(),lng:e.lnglat.getLng()});renderMarkers();document.getElementById('info').innerText='已添加:'+n;});}}});
function saveMarkers(){allMarkers.forEach(m=>{fetch('/update_marker',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_name:m.name,new_name:m.name,lat:m.lat,lng:m.lng})});});alert('保存成功！');}
</script></body></html>
"""

# ========== Kivy 界面 ==========
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.utils import platform
import threading

class NaviApp(App):
    def build(self):
        self.title = '骑手导航'
        self.instructions = []  # 存储导航指令
        
        threading.Thread(target=start_server, daemon=True).start()
        time.sleep(1)
        with open('interactive_map.html','w',encoding='utf-8') as f:
            f.write(INTERACTIVE_MAP_HTML.replace('__AMAP_KEY__', AMAP_KEY))
        
        root = BoxLayout(orientation='vertical', padding=10, spacing=8)
        root.add_widget(Label(text='🛵 骑手导航', size_hint=(1,0.06), font_size='18sp', bold=True))
        root.add_widget(Label(text='输入地址（逗号分隔）:', size_hint=(1,0.04), halign='left'))
        self.addr_input = TextInput(text='双桥万达广场,长楹天街', size_hint=(1,0.12), font_size='14sp', multiline=True)
        root.add_widget(self.addr_input)
        
        # 语音输入按钮
        voice_btn_box = BoxLayout(orientation='horizontal', size_hint=(1,0.06), spacing=5)
        self.voice_btn = Button(text='🎤 语音输入', font_size='14sp', background_color=(0.9,0.3,0.5,1))
        self.voice_btn.bind(on_press=self.voice_input)
        voice_btn_box.add_widget(self.voice_btn)
        voice_btn_box.add_widget(Label(text='', size_hint=(0.7,1)))
        root.add_widget(voice_btn_box)
        
        night_box = BoxLayout(orientation='horizontal', size_hint=(1,0.05), spacing=5)
        self.night_check = CheckBox(size_hint=(0.1,1))
        night_box.add_widget(Label(text='🌙夜间', size_hint=(0.2,1)))
        night_box.add_widget(self.night_check)
        self.speak_check = CheckBox(size_hint=(0.1,1))
        night_box.add_widget(Label(text='🔊播报', size_hint=(0.2,1)))
        night_box.add_widget(self.speak_check)
        night_box.add_widget(Label(text='', size_hint=(0.4,1)))
        root.add_widget(night_box)
        
        btn_box = BoxLayout(orientation='horizontal', size_hint=(1,0.08), spacing=5)
        self.start_btn = Button(text='🚀导航', font_size='14sp')
        self.start_btn.bind(on_press=self.start_navi)
        self.map_btn = Button(text='🗺️地图', font_size='14sp')
        self.map_btn.bind(on_press=self.open_map)
        self.mark_btn = Button(text='📍标记', font_size='14sp')
        self.mark_btn.bind(on_press=self.open_marker_map)
        self.speak_btn = Button(text='🔊播报', font_size='14sp')
        self.speak_btn.bind(on_press=self.speak_instructions)
        btn_box.add_widget(self.start_btn)
        btn_box.add_widget(self.map_btn)
        btn_box.add_widget(self.mark_btn)
        btn_box.add_widget(self.speak_btn)
        root.add_widget(btn_box)
        
        self.log_label = Label(text='等待操作...', size_hint=(1,0.5), font_size='12sp', halign='left', valign='top')
        self.log_label.bind(size=self.log_label.setter('text_size'))
        scroll = ScrollView(size_hint=(1,0.5))
        scroll.add_widget(self.log_label)
        root.add_widget(scroll)
        return root
    
    def log(self, msg):
        c = self.log_label.text
        if c == '等待操作...': c = ''
        self.log_label.text = c + msg + '\n'
    
    def voice_input(self, instance):
        """语音输入 - 调用安卓语音识别"""
        if platform == 'android':
            from android.permissions import request_permissions, Permission
            request_permissions([Permission.RECORD_AUDIO])
            
            from jnius import autoclass
            RecognizerIntent = autoclass('android.speech.RecognizerIntent')
            SpeechRecognizer = autoclass('android.speech.SpeechRecognizer')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Intent = autoclass('android.content.Intent')
            
            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "说出目的地")
            
            try:
                PythonActivity.mActivity.startActivityForResult(intent, 1001)
                self.log('🎤 请说话...')
            except Exception as e:
                self.log(f'语音输入错误: {e}')
        else:
            self.log('⚠ 语音输入仅支持安卓设备')
    
    def on_activity_result(self, request_code, result_code, data):
        """接收语音识别结果"""
        from jnius import autoclass
        RecognizerIntent = autoclass('android.speech.RecognizerIntent')
        if request_code == 1001 and result_code == -1:
            results = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            if results and len(results) > 0:
                text = results[0]
                cur = self.addr_input.text
                self.addr_input.text = cur + ',' + text if cur else text
                self.log(f'✅ 识别: {text}')
    
    def speak_text(self, text):
        """语音播报 - 调用安卓 TTS"""
        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                TextToSpeech = autoclass('android.speech.tts.TextToSpeech')
                tts = TextToSpeech(PythonActivity.mActivity, None)
                tts.setSpeechRate(0.9)
                tts.speak(text, TextToSpeech.QUEUE_FLUSH, None, None)
            except Exception as e:
                self.log(f'播报错误: {e}')
        else:
            self.log('⚠ 语音播报仅支持安卓设备')
    
    def speak_instructions(self, instance):
        """播报导航指令"""
        if hasattr(self, 'instructions') and self.instructions:
            self.speak_text("开始导航")
            for instr in self.instructions:
                self.speak_text(instr)
                time.sleep(0.3)
            self.speak_text("配送完成")
        else:
            self.log('⚠ 先生成路线')
    
    def start_navi(self, instance):
        self.start_btn.disabled = True
        self.start_btn.text = '计算中...'
        try:
            night_mode = self.night_check.active
            addrs = [a.strip() for a in self.addr_input.text.strip().split(",") if a.strip()]
            if len(addrs) < 2: self.log('❌ 至少2个地址'); return
            
            self.log('查询坐标...')
            coords_dict = {}
            for a in addrs:
                c = address_to_coord(a, night_mode)
                if c: coords_dict[a] = c; self.log(f'  ✅ {a}')
                else: self.log(f'  ❌ {a}')
            if len(coords_dict) < 2: self.log('❌ 地址不足'); return
            
            self.log('计算路线...')
            best_order, total = find_best_order(coords_dict)
            self.log(f'距离约:{total}米')
            
            route_memory = load_json(ROUTE_MEMORY_FILE)
            detours = load_json(DETOUR_FILE)
            all_coords, self.instructions = get_multi_route(best_order, coords_dict, route_memory, detours, night_mode)
            
            if not all_coords: self.log('❌ 路线失败'); return
            
            blockages = load_json(BLOCKAGE_FILE)
            map_file = draw_map_html(all_coords, coords_dict, best_order, night_mode, blockages, detours)
            self.last_map = map_file
            self.log(f'✅ 地图已生成')
            
            # 自动播报
            if self.speak_check.active:
                threading.Thread(target=self._auto_speak, daemon=True).start()
        except Exception as e:
            self.log(f'❌ {e}')
        finally:
            self.start_btn.disabled = False; self.start_btn.text = '🚀导航'
    
    def _auto_speak(self):
        self.speak_text("导航开始")
        for instr in self.instructions:
            self.speak_text(instr)
            time.sleep(0.3)
        self.speak_text("配送完成")
    
    def open_map(self, instance):
        if hasattr(self,'last_map') and os.path.exists(self.last_map):
            webbrowser.open('file://'+self.last_map)
        else:
            self.log('⚠ 先生成地图')
    
    def open_marker_map(self, instance):
        webbrowser.open(f'http://localhost:{MAP_PORT}/map')

if __name__ == '__main__':
    NaviApp().run()