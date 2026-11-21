/* MiauEditor2D —— 面向延展方向的轨道切面编辑器
 *
 * 视角：站在轨道末端、沿 -X 看过去，看到的是 X-Y 切面。
 *   横向 = 歌曲延展方向（1 格 = 0.5 tick，标尺按 tick 标注）
 *   纵向 = 高度 Y（向上为正）
 * 由于切面看不到 Z，拖动只能改高度；横向偏移仍在右侧面板或 3D 视图中调整。
 */
(function () {
  'use strict';

  const MIN_SCALE = 1.6;
  const MAX_SCALE = 48;
  const MAX_POINTS_PER_LANE = 9000;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  const WOOL_COLORS = {
    white: '#e9e9e9', light_gray: '#a2a2a2', gray: '#4a4a4a', black: '#1b1b1b',
    red: '#b03a34', orange: '#e0782a', yellow: '#f0c53c', lime: '#7ab73a',
    green: '#5a7a34', cyan: '#2f8f92', light_blue: '#6cb8d8', blue: '#33469b',
    purple: '#7b3aa8', magenta: '#c055c0', pink: '#e79ab2', brown: '#6d4b2d',
  };

  function hashColor(text) {
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      hash = (hash * 31 + text.charCodeAt(i)) & 0xffffff;
    }
    const r = 70 + (hash & 0x7f);
    const g = 70 + ((hash >> 8) & 0x7f);
    const b = 70 + ((hash >> 16) & 0x5f);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  const COLOR_CACHE = new Map();

  function colorOf(block) {
    const id = String(block || '').split('[')[0].replace('minecraft:', '');
    const cached = COLOR_CACHE.get(id);
    if (cached) return cached;
    let color;
    if (id.indexOf('command_block') >= 0) color = '#c8763c';
    else if (id.indexOf('repeater') >= 0 || id.indexOf('comparator') >= 0) color = '#8a8073';
    else if (id.indexOf('redstone') >= 0) color = '#c0392b';
    else if (id.indexOf('note_block') >= 0) color = '#9a6440';
    else if (id.indexOf('_wool') >= 0) color = WOOL_COLORS[id.replace('_wool', '')] || '#dedede';
    else if (id.indexOf('planks') >= 0 || id.indexOf('log') >= 0) color = '#b08a5a';
    else if (id.indexOf('ice') >= 0) color = '#9fd8ee';
    else if (id.indexOf('glass') >= 0) color = 'rgba(190,225,235,0.65)';
    else if (id.indexOf('stone') >= 0) color = '#8d8d8d';
    else if (id.indexOf('redstone_lamp') >= 0) color = '#b07a45';
    else color = hashColor(id);
    COLOR_CACHE.set(id, color);
    return color;
  }

  class MiauEditor2D {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.options = options || {};
      this.markers = [];
      this.palette = [];
      this.blocks = [];
      this.selected = null;
      this.bounds = null;
      this.scale = 10;
      this.tx = 54;
      this.ty = 200;
      this.pointers = new Map();
      this.drag = null;
      this.pinch = null;
      this.lanes = new Map();
      this._userMoved = false;
      this._bind();
      this.resize();
    }

    _bind() {
      const canvas = this.canvas;
      canvas.addEventListener('contextmenu', (event) => event.preventDefault());
      canvas.addEventListener('pointerdown', (event) => this._pointerDown(event));
      canvas.addEventListener('pointermove', (event) => this._pointerMove(event));
      canvas.addEventListener('pointerup', (event) => this._pointerUp(event));
      canvas.addEventListener('pointercancel', (event) => this._pointerUp(event));
      canvas.addEventListener('wheel', (event) => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        this._zoomAt(event.clientX - rect.left, event.clientY - rect.top,
          Math.exp(-event.deltaY * 0.0016));
      }, { passive: false });
    }

    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || 600;
      const height = this.canvas.clientHeight || 400;
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.width = width;
      this.height = height;
      this.draw();
    }

    setData(bounds, blocks, palette) {
      this.bounds = bounds || null;
      if (Array.isArray(blocks)) this.blocks = blocks;
      if (Array.isArray(palette)) this.palette = palette;
      this.lanes.clear();
      if (!this._userMoved) this.fit();
      else this.draw();
    }

    setMarkers(items, selected) {
      this.markers = items || [];
      if (selected !== undefined) this.selected = selected;
      this.lanes.clear();
      if (!this._userMoved) this.fit();
      else this.draw();
    }

    setSelected(gid) {
      this.selected = gid;
      this.draw();
    }

    focusMarker(gid) {
      const marker = this.markers.find((item) => item.gid === gid);
      if (!marker) return;
      this.selected = gid;
      const px = this.tx + (marker.x + 0.5) * this.scale;
      const py = this.ty - (marker.y + 0.5) * this.scale;
      const margin = 70;
      if (px < margin || px > this.width - margin || py < margin || py > this.height - margin) {
        this.tx = this.width / 2 - (marker.x + 0.5) * this.scale;
        this.ty = this.height / 2 + (marker.y + 0.5) * this.scale;
        this._userMoved = true;
      }
      this.draw();
    }

    updateMarker(gid, position) {
      const marker = this.markers.find((item) => item.gid === gid);
      if (!marker) return;
      if (position.y !== undefined) marker.y = position.y;
      if (position.side !== undefined) {
        if (marker.side !== position.side) this.lanes.delete(marker.side);
        marker.side = position.side;
        marker.z = position.side;
      }
      this.draw();
    }

    fit() {
      if (!this.width || !this.height) return;
      const endX = this.bounds && this.bounds.max ? this.bounds.max[0] : 120;
      const minX = -2;
      const maxX = Math.max(24, Math.min(endX + 4, 400));
      let minY = 0;
      let maxY = 8;
      if (this.markers.length) {
        minY = Math.min(...this.markers.map((item) => item.y)) - 2;
        maxY = Math.max(...this.markers.map((item) => item.y)) + 5;
      }
      if (this.bounds && this.bounds.min) {
        minY = Math.min(minY, this.bounds.min[1] - 1);
        maxY = Math.max(maxY, this.bounds.max[1] + 2);
      }
      if (maxY - minY < 10) maxY = minY + 10;
      const sx = (this.width - 74) / (maxX - minX);
      const sy = (this.height - 64) / (maxY - minY);
      this.scale = clamp(Math.min(sx, sy), MIN_SCALE, MAX_SCALE);
      this.tx = 46 - minX * this.scale;
      this.ty = this.height - 34 + minY * this.scale;
      this._userMoved = false;
      this.options.onViewChange && this.options.onViewChange(this.scale);
      this.draw();
    }

    zoomBy(factor) {
      this._zoomAt(this.width / 2, this.height / 2, factor);
    }

    _zoomAt(sx, sy, factor) {
      const next = clamp(this.scale * factor, MIN_SCALE, MAX_SCALE);
      if (next === this.scale) return;
      const wx = (sx - this.tx) / this.scale;
      const wy = (this.ty - sy) / this.scale;
      this.scale = next;
      this.tx = sx - wx * next;
      this.ty = sy + wy * next;
      this._userMoved = true;
      this.options.onViewChange && this.options.onViewChange(this.scale);
      this.draw();
    }

    _hitMarker(sx, sy) {
      let best = null;
      let bestDist = 22;
      for (const marker of this.markers) {
        const px = this.tx + (marker.x + 0.5) * this.scale;
        const py = this.ty - (marker.y + 0.5) * this.scale;
        const dist = Math.hypot(px - sx, py - sy);
        if (dist < bestDist) {
          bestDist = dist;
          best = marker;
        }
      }
      return best;
    }

    _pointerDown(event) {
      if (event.button === 1) event.preventDefault();
      this.canvas.setPointerCapture(event.pointerId);
      const rect = this.canvas.getBoundingClientRect();
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      this.pointers.set(event.pointerId, point);
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.pinch = {
          distance: Math.hypot(a.x - b.x, a.y - b.y) || 1,
          center: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
          scale: this.scale,
          tx: this.tx,
          ty: this.ty,
        };
        this.drag = null;
        return;
      }
      const marker = this._hitMarker(point.x, point.y);
      if (marker) {
        this.drag = {
          type: 'marker',
          gid: marker.gid,
          lastY: point.y,
          baseY: marker.y,
          y: marker.y,
        };
        this.selected = marker.gid;
        this.options.onSelect && this.options.onSelect(marker.gid);
      } else {
        this.drag = { type: 'pan', lastX: point.x, lastY: point.y };
      }
      this.draw();
    }

    _pointerMove(event) {
      const rect = this.canvas.getBoundingClientRect();
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      if (this.pointers.has(event.pointerId)) this.pointers.set(event.pointerId, point);

      if (this.pointers.size === 2 && this.pinch) {
        const [a, b] = [...this.pointers.values()];
        const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const center = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const next = clamp(this.pinch.scale * (distance / this.pinch.distance),
          MIN_SCALE, MAX_SCALE);
        const world = {
          x: (this.pinch.center.x - this.pinch.tx) / this.pinch.scale,
          y: (this.pinch.ty - this.pinch.center.y) / this.pinch.scale,
        };
        this.scale = next;
        this.tx = center.x - world.x * next;
        this.ty = center.y + world.y * next;
        this._userMoved = true;
        this.draw();
        return;
      }

      if (!this.drag) return;
      if (this.drag.type === 'pan') {
        this.tx += point.x - this.drag.lastX;
        this.ty += point.y - this.drag.lastY;
        this.drag.lastX = point.x;
        this.drag.lastY = point.y;
        this._userMoved = true;
        this.draw();
        return;
      }

      const marker = this.markers.find((item) => item.gid === this.drag.gid);
      if (!marker) return;
      const dy = (this.drag.lastY - point.y) / this.scale;
      this.drag.lastY = point.y;
      const y = clamp(Math.round(this.drag.y + dy), -24, 96);
      if (y !== this.drag.y) {
        this.drag.y = y;
        marker.y = y;
        this.draw();
        this.options.onMarkerMove &&
          this.options.onMarkerMove(this.drag.gid, { y, side: marker.side }, false);
      }
    }

    _pointerUp(event) {
      this.pointers.delete(event.pointerId);
      if (this.pointers.size < 2) this.pinch = null;
      if (!this.drag) return;
      if (this.drag.type === 'marker') {
        const marker = this.markers.find((item) => item.gid === this.drag.gid);
        if (marker) {
          this.options.onMarkerMove &&
            this.options.onMarkerMove(this.drag.gid,
              { y: marker.y, side: marker.side }, true);
        }
      }
      this.drag = null;
      this.draw();
    }

    // ── 绘制 ───────────────────────────────────────────────
    draw() {
      const ctx = this.ctx;
      if (!ctx || !this.width) return;
      const width = this.width;
      const height = this.height;
      ctx.clearRect(0, 0, width, height);

      const bg = ctx.createLinearGradient(0, 0, 0, height);
      bg.addColorStop(0, '#101317');
      bg.addColorStop(1, '#0a0c0f');
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, width, height);

      this._drawGrid(ctx, width, height);
      this._drawStructure(ctx);
      this._drawMarkers(ctx);
      this._drawAxes(ctx, height);
    }

    _drawGrid(ctx, width, height) {
      const step = this.scale;
      const startX = Math.floor((-this.tx) / step) - 1;
      const endX = Math.ceil((width - this.tx) / step) + 1;
      const startY = Math.floor((this.ty - height) / step) - 1;
      const endY = Math.ceil(this.ty / step) + 1;

      for (let x = startX; x <= endX; x++) {
        const px = Math.round(this.tx + x * step) + 0.5;
        const major = x % 8 === 0;
        if (!major && step < 5) continue;
        ctx.strokeStyle = major ? 'rgba(255,139,61,0.15)' : 'rgba(255,255,255,0.04)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, height);
        ctx.stroke();
      }
      for (let y = startY; y <= endY; y++) {
        if (y % 4 !== 0 && step < 6) continue;
        const py = Math.round(this.ty - y * step) + 0.5;
        ctx.strokeStyle = y === 0 ? 'rgba(120,220,220,0.32)' : 'rgba(255,255,255,0.045)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, py);
        ctx.lineTo(width, py);
        ctx.stroke();
      }
    }

    _lanePoints(marker) {
      const key = marker.side;
      if (this.lanes.has(key)) return this.lanes.get(key);
      const points = [];
      const scale = this.scale;
      const blocks = this.blocks;
      if (blocks.length && scale >= 3.5) {
        for (let i = 0; i < blocks.length; i++) {
          const block = blocks[i];
          if (block[2] !== marker.side) continue;
          points.push(block[0], block[1], block[3]);
          if (points.length >= MAX_POINTS_PER_LANE * 3) break;
        }
      }
      this.lanes.set(key, points);
      return points;
    }

    _drawStructure(ctx) {
      if (this.scale < 3.5) return;
      const size = Math.max(1, Math.min(this.scale, 6));
      for (const marker of this.markers) {
        const points = this._lanePoints(marker);
        for (let i = 0; i < points.length; i += 3) {
          const block = this.palette[points[i + 2]] || '';
          ctx.fillStyle = colorOf(block);
          const px = this.tx + points[i] * this.scale;
          const py = this.ty - points[i + 1] * this.scale;
          if (px < -size || px > this.width || py < -size || py > this.height) continue;
          ctx.fillRect(px, py - size, size, size);
        }
      }
    }

    _drawMarkers(ctx) {
      for (const marker of this.markers) {
        const px = this.tx + (marker.x + 0.5) * this.scale;
        const py = this.ty - (marker.y + 0.5) * this.scale;
        const selected = this.selected === marker.gid;

        // 轨道基准线：从起点延伸到结构末端
        const endX = this.bounds && this.bounds.max ? this.bounds.max[0] : marker.x + 60;
        ctx.save();
        ctx.strokeStyle = selected ? 'rgba(255,139,61,0.45)' : 'rgba(255,255,255,0.10)';
        ctx.setLineDash([6, 7]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(this.tx + endX * this.scale, py);
        ctx.stroke();
        ctx.restore();

        const radius = selected ? 11 : 9;
        ctx.save();
        ctx.shadowColor = selected ? 'rgba(255,139,61,0.75)' : 'rgba(120,220,220,0.45)';
        ctx.shadowBlur = selected ? 16 : 8;
        ctx.fillStyle = selected ? '#ff8b3d' : '#1d2b2f';
        ctx.strokeStyle = selected ? '#ffd1ad' : 'rgba(120,220,220,0.85)';
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.roundRect(px - radius, py - radius, radius * 2, radius * 2, 4);
        ctx.fill();
        ctx.stroke();
        ctx.restore();

        ctx.fillStyle = selected ? '#231206' : '#d7f4f4';
        ctx.font = '600 10px "IBM Plex Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(marker.gid + 1), px, py - 0.5);

        ctx.fillStyle = selected ? 'rgba(255,209,173,0.95)' : 'rgba(215,244,244,0.72)';
        ctx.font = '400 10px "IBM Plex Mono", monospace';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'bottom';
        ctx.fillText('Y ' + marker.y + ' · Z ' + marker.side, px + radius + 6, py + 4);
      }
    }

    _drawAxes(ctx, height) {
      const step = this.scale;
      ctx.fillStyle = 'rgba(255,255,255,0.4)';
      ctx.font = '400 9px "IBM Plex Mono", monospace';

      const startX = Math.floor((-this.tx) / step);
      const endX = Math.ceil((this.width - this.tx) / step);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (let x = startX; x <= endX; x += 1) {
        if (x % 8 !== 0) continue;
        const px = Math.round(this.tx + x * step) + 0.5;
        ctx.fillRect(px, height - 9, 1, 5);
        ctx.fillText('t' + Math.round(x / 2), px, height - 22);
      }

      const startY = Math.floor((this.ty - height) / step);
      const endY = Math.ceil(this.ty / step);
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (let y = startY; y <= endY; y += 4) {
        const py = Math.round(this.ty - y * step) + 0.5;
        ctx.fillRect(30, py, 5, 1);
        ctx.fillText('Y' + y, 27, py);
      }

      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillStyle = 'rgba(255,139,61,0.7)';
      ctx.font = '600 9.5px "IBM Plex Mono", monospace';
      ctx.fillText('切面 X → / Y ↑', 34, 12);
    }

    dispose() {
      this.markers = [];
      this.blocks = [];
    }
  }

  window.MiauEditor2D = MiauEditor2D;
})();
