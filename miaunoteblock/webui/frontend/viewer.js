/* MiauViewer · 极简 WebGL2 体素查看器
   依赖：无。方块纹理从 /api/texture 取，运行时拼成图集。
   交互：左键旋转 / 右键或 Shift 平移 / 滚轮缩放 / 触屏单指旋转双指缩放平移。 */
(function () {
  'use strict';

  const TILE = 32;
  const COLS = 32;
  const ATLAS = TILE * COLS;

  const VERT = `#version 300 es
  precision highp float;
  layout(location=0) in vec3 aPos;
  layout(location=1) in vec3 aNormal;
  layout(location=2) in vec2 aUV;
  layout(location=3) in vec3 iPos;
  layout(location=4) in float iTile;
  layout(location=5) in vec3 iScale;
  uniform mat4 uViewProj;
  out vec2 vUV;
  out vec3 vNormal;
  out float vTile;
  void main() {
    vec3 world = iPos + aPos * iScale;
    gl_Position = uViewProj * vec4(world, 1.0);
    vUV = aUV;
    vNormal = aNormal;
    vTile = iTile;
  }`;

  const FRAG = `#version 300 es
  precision highp float;
  in vec2 vUV;
  in vec3 vNormal;
  in float vTile;
  uniform sampler2D uAtlas;
  uniform float uCols;
  uniform vec3 uLight;
  out vec4 outColor;
  void main() {
    float col = mod(vTile, uCols);
    float row = floor(vTile / uCols);
    vec2 uv = (vec2(col, row) + clamp(vUV, 0.012, 0.988)) / uCols;
    vec4 tex = texture(uAtlas, uv);
    if (tex.a < 0.06) discard;
    float diff = max(dot(normalize(vNormal), normalize(uLight)), 0.0);
    float light = 0.62 + 0.42 * diff;
    outColor = vec4(tex.rgb * light, tex.a);
  }`;

  const MARK_VERT = `#version 300 es
  precision highp float;
  layout(location=0) in vec3 aPos;
  layout(location=3) in vec3 iPos;
  layout(location=5) in vec3 iScale;
  uniform mat4 uViewProj;
  void main() {
    vec3 world = iPos + (aPos - 0.5) * iScale + 0.5;
    gl_Position = uViewProj * vec4(world, 1.0);
  }`;

  const MARK_FRAG = `#version 300 es
  precision highp float;
  uniform vec4 uColor;
  out vec4 outColor;
  void main() { outColor = uColor; }`;

  const bitmapCache = new Map();

  // ── 矩阵工具（列主序）────────────────────────────────────
  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }

  function lookAt(eye, center, up) {
    let zx = eye[0] - center[0], zy = eye[1] - center[1], zz = eye[2] - center[2];
    let len = Math.hypot(zx, zy, zz) || 1;
    zx /= len; zy /= len; zz /= len;
    let xx = up[1] * zz - up[2] * zy;
    let xy = up[2] * zx - up[0] * zz;
    let xz = up[0] * zy - up[1] * zx;
    len = Math.hypot(xx, xy, xz) || 1;
    xx /= len; xy /= len; xz /= len;
    const yx = zy * xz - zz * xy;
    const yy = zz * xx - zx * xz;
    const yz = zx * xy - zy * xx;
    return new Float32Array([
      xx, yx, zx, 0,
      xy, yy, zy, 0,
      xz, yz, zz, 0,
      -(xx * eye[0] + xy * eye[1] + xz * eye[2]),
      -(yx * eye[0] + yy * eye[1] + yz * eye[2]),
      -(zx * eye[0] + zy * eye[1] + zz * eye[2]),
      1,
    ]);
  }

  function multiply(a, b) {
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c++) {
      for (let r = 0; r < 4; r++) {
        out[c * 4 + r] =
          a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] +
          a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
      }
    }
    return out;
  }

  // ── 着色器 ───────────────────────────────────────────────
  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error('着色器编译失败：' + log);
    }
    return shader;
  }

  function program(gl, vert, frag) {
    const p = gl.createProgram();
    gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vert));
    gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, frag));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error('着色器链接失败：' + gl.getProgramInfoLog(p));
    }
    return p;
  }

  // ── 方块外形提示 ─────────────────────────────────────────
  // 纹理只按方块 ID 取用：去掉 blockstate 与 NBT，避免长命令串触发请求失败
  function blockKey(block) {
    return String(block).split('[')[0].split('{')[0];
  }

  function shapeOf(block) {
    const id = String(block).split('[')[0];
    if (id === 'minecraft:redstone_wire') return { y: 0.9, h: 0.1 };
    if (id.indexOf('repeater') >= 0) return { y: 0.875, h: 0.125 };
    if (id.indexOf('comparator') >= 0) return { y: 0.875, h: 0.125 };
    if (/_button$/.test(id)) return { y: 0, h: 0.25, shrink: 0.3 };
    if (/_slab$/.test(id)) return { y: 0, h: 0.5 };
    if (/_carpet$/.test(id)) return { y: 0, h: 0.0625 };
    if (/_pressure_plate$/.test(id)) return { y: 0, h: 0.0625 };
    if (/_trapdoor$/.test(id)) return { y: 0.8125, h: 0.1875, shrink: 0.1 };
    if (/_stairs$/.test(id)) return { y: 0, h: 1 };
    if (id === 'minecraft:snow' || id === 'minecraft:snow_layer') return { y: 0, h: 0.125 };
    if (id === 'minecraft:lantern' || id === 'minecraft:soul_lantern') {
      return { y: 0, h: 0.5, shrink: 0.25 };
    }
    if (id === 'minecraft:redstone_torch' || id === 'minecraft:torch' ||
        id === 'minecraft:wall_torch') return { y: 0, h: 0.6, shrink: 0.34 };
    return { y: 0, h: 1, shrink: 0 };
  }

  class MiauViewer {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.options = options || {};
      this.wrap = canvas.parentElement;
      this.blocks = [];
      this.palette = [];
      this.markers = [];
      this.selected = null;
      this.camera = { target: [0, 0, 0], yaw: -0.72, pitch: 0.52, distance: 40 };
      this.bounds = { min: [0, 0, 0], max: [0, 0, 0], size: [0, 0, 0] };
      this.spin = false;
      this.needsRender = true;
      this.pointers = new Map();
      this.mode = null;
      this.drag = null;
      this.lastFrame = performance.now();
      this.blockCount = 0;
      this.markerCount = 0;
      this.__raf = 0;
      this.supported = false;

      const gl = canvas.getContext('webgl2', {
        antialias: true,
        alpha: false,
        depth: true,
        powerPreference: 'high-performance',
      });
      if (!gl) {
        canvas.insertAdjacentHTML('afterend',
          '<div class="viewport-loading"><p>当前浏览器不支持 WebGL2，无法显示 3D 预览。</p></div>');
        return;
      }
      this.gl = gl;
      this.supported = true;
      this.cubeProgram = program(gl, VERT, FRAG);
      this.markerProgram = program(gl, MARK_VERT, MARK_FRAG);
      this.uniforms = {
        viewProj: gl.getUniformLocation(this.cubeProgram, 'uViewProj'),
        atlas: gl.getUniformLocation(this.cubeProgram, 'uAtlas'),
        cols: gl.getUniformLocation(this.cubeProgram, 'uCols'),
        light: gl.getUniformLocation(this.cubeProgram, 'uLight'),
      };
      this.markerUniforms = {
        viewProj: gl.getUniformLocation(this.markerProgram, 'uViewProj'),
        color: gl.getUniformLocation(this.markerProgram, 'uColor'),
      };
      this._initGeometry();
      this._initAtlas();
      this._bindEvents();
      this.resize();
      this._loop = this._loop.bind(this);
      this.__raf = requestAnimationFrame(this._loop);
    }

    // ── 初始化 ─────────────────────────────────────────────
    _initGeometry() {
      const gl = this.gl;
      const positions = [];
      const normals = [];
      const uvs = [];
      const indices = [];
      const faces = [
        { n: [1, 0, 0], v: [[1, 0, 0], [1, 0, 1], [1, 1, 1], [1, 1, 0]] },
        { n: [-1, 0, 0], v: [[0, 0, 1], [0, 0, 0], [0, 1, 0], [0, 1, 1]] },
        { n: [0, 1, 0], v: [[0, 1, 1], [1, 1, 1], [1, 1, 0], [0, 1, 0]] },
        { n: [0, -1, 0], v: [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]] },
        { n: [0, 0, 1], v: [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]] },
        { n: [0, 0, -1], v: [[1, 0, 0], [0, 0, 0], [0, 1, 0], [1, 1, 0]] },
      ];
      const uv = [[0, 1], [1, 1], [1, 0], [0, 0]];
      faces.forEach((face, fi) => {
        face.v.forEach((vertex, vi) => {
          positions.push(vertex[0], vertex[1], vertex[2]);
          normals.push(face.n[0], face.n[1], face.n[2]);
          uvs.push(uv[vi][0], uv[vi][1]);
        });
        const base = fi * 4;
        indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
      });

      this.geometry = {
        positions: new Float32Array(positions),
        normals: new Float32Array(normals),
        uvs: new Float32Array(uvs),
        indices: new Uint16Array(indices),
      };

      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      this._staticBuffer(0, this.geometry.positions, 3);
      this._staticBuffer(1, this.geometry.normals, 3);
      this._staticBuffer(2, this.geometry.uvs, 2);
      const ibo = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, this.geometry.indices, gl.STATIC_DRAW);
      this.indexBuffer = ibo;

      this.instanceBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceBuffer);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 3, gl.FLOAT, false, 28, 0);
      gl.vertexAttribDivisor(3, 1);
      gl.enableVertexAttribArray(4);
      gl.vertexAttribPointer(4, 1, gl.FLOAT, false, 28, 12);
      gl.vertexAttribDivisor(4, 1);
      gl.enableVertexAttribArray(5);
      gl.vertexAttribPointer(5, 3, gl.FLOAT, false, 28, 16);
      gl.vertexAttribDivisor(5, 1);

      this.markerVao = gl.createVertexArray();
      gl.bindVertexArray(this.markerVao);
      this._staticBuffer(0, this.geometry.positions, 3);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
      this.markerBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.markerBuffer);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 3, gl.FLOAT, false, 24, 0);
      gl.vertexAttribDivisor(3, 1);
      gl.enableVertexAttribArray(5);
      gl.vertexAttribPointer(5, 3, gl.FLOAT, false, 24, 12);
      gl.vertexAttribDivisor(5, 1);

      gl.bindVertexArray(null);
      gl.enable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.clearColor(0.028, 0.032, 0.04, 1);
    }

    _staticBuffer(location, data, size) {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
      return buffer;
    }

    _initAtlas() {
      const gl = this.gl;
      this.atlasCanvas = document.createElement('canvas');
      this.atlasCanvas.width = ATLAS;
      this.atlasCanvas.height = ATLAS;
      this.atlasCtx = this.atlasCanvas.getContext('2d');
      // 先用纯黑填充，避免未上传时采样到垃圾
      this.atlasCtx.fillStyle = '#1a1d24';
      this.atlasCtx.fillRect(0, 0, ATLAS, ATLAS);
      this.atlasTexture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.atlasTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE,
        this.atlasCanvas);
    }

    // ── 数据 ───────────────────────────────────────────────
    setData(palette, blocks, bounds) {
      if (!this.supported) return;
      this.palette = palette || [];
      this.blocks = blocks || [];
      this.bounds = bounds || this.bounds;
      this.blockCount = this.blocks.length;
      this.needsRender = true;
      this._buildTextureIndex();
      const baseTile = 0;
      this._buildInstances(baseTile);
      this._loadAtlas();
      if (!this._userMovedCamera) this.resetCamera();
      this.requestRender();
    }

    // 方块字符串（含 blockstate / NBT）可能数以千计，但纹理只有几十种。
    // 这里把每个方块映射到「纹理键」索引，避免图集被命令方块的长 NBT 撑爆。
    _buildTextureIndex() {
      const indexOf = new Map();
      const keys = [];
      const mapping = new Uint16Array(this.palette.length);
      this.palette.forEach((block, index) => {
        const key = blockKey(block);
        let slot = indexOf.get(key);
        if (slot === undefined) {
          slot = keys.length;
          indexOf.set(key, slot);
          keys.push(key);
        }
        mapping[index] = slot;
      });
      this.texKeys = keys;
      this.blockTexIndex = mapping;
    }

    _buildInstances() {
      const gl = this.gl;
      const mapping = this.blockTexIndex || [];
      const shapes = new Map();
      const count = this.blocks.length;
      const data = new Float32Array(count * 7);
      for (let i = 0; i < count; i++) {
        const b = this.blocks[i];
        const block = this.palette[b[3]] || '';
        let shape = shapes.get(block);
        if (!shape) {
          shape = shapeOf(block);
          shapes.set(block, shape);
        }
        const o = i * 7;
        data[o] = b[0] + shape.shrink;
        data[o + 1] = b[1] + shape.y;
        data[o + 2] = b[2] + shape.shrink;
        data[o + 3] = mapping[b[3]] || 0;
        data[o + 4] = 1 - shape.shrink * 2;
        data[o + 5] = shape.h;
        data[o + 6] = 1 - shape.shrink * 2;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    }

    _loadAtlas() {
      const palette = this.texKeys || [];
      if (!palette.length) return;
      const token = this.options.token;
      const ctx = this.atlasCtx;
      const tasks = palette.map((key, index) => {
        const cached = bitmapCache.get(key);
        const col = index % COLS;
        const row = Math.floor(index / COLS);
        // 红石粉在资源里是灰色细线纹理，直接铺在 0.1 高的平面上几乎看不见。
        // 这里改成「纯红平面」：一条红色主线 + 略暗的边框，稳定可辨。
        if (key.indexOf('redstone_wire') >= 0) {
          ctx.imageSmoothingEnabled = false;
          ctx.fillStyle = '#c62c26';
          ctx.fillRect(col * TILE, row * TILE, TILE, TILE);
          ctx.fillStyle = '#8f1e1a';
          ctx.fillRect(col * TILE, row * TILE, TILE, 2);
          ctx.fillRect(col * TILE, row * TILE + TILE - 2, TILE, 2);
          ctx.fillRect(col * TILE, row * TILE, 2, TILE);
          ctx.fillRect(col * TILE + TILE - 2, row * TILE, 2, TILE);
          ctx.fillStyle = '#e8504a';
          ctx.fillRect(col * TILE + 4, row * TILE + 4, TILE - 8, TILE - 8);
          return Promise.resolve(null);
        }
        const draw = (bitmap) => {
          ctx.imageSmoothingEnabled = false;
          // 动画纹理是竖排帧，只取第一帧的方形区域
          const src = Math.min(bitmap.width, bitmap.height) || 16;
          try {
            ctx.drawImage(bitmap, 0, 0, src, src,
              col * TILE, row * TILE, TILE, TILE);
            // 红石火把 / 红石块等灰色资源统一染红
            if (key.indexOf('redstone') >= 0) {
              ctx.globalCompositeOperation = 'source-atop';
              ctx.fillStyle = 'rgba(214, 44, 38, 0.92)';
              ctx.fillRect(col * TILE, row * TILE, TILE, TILE);
              ctx.globalCompositeOperation = 'source-over';
            }
          } catch (err) { /* 忽略个别坏图 */ }
        };
        if (cached) {
          return cached.then(draw).catch(() => draw(_colorTile(key)));
        }
        const promise = fetch('/api/texture?block=' + encodeURIComponent(key), {
          headers: { 'X-Miau-Token': token },
        }).then((response) => {
          if (!response.ok) throw new Error('texture ' + response.status);
          return response.blob();
        }).then((blob) => createImageBitmap(blob))
          .then((bitmap) => {
            bitmapCache.set(key, Promise.resolve(bitmap));
            draw(bitmap);
            return null;
          })
          .catch(() => {
            bitmapCache.set(key, Promise.reject(new Error('missing')));
            draw(_colorTile(key));
          });
        return promise;
      });
      Promise.all(tasks).then(() => {
        const gl = this.gl;
        gl.bindTexture(gl.TEXTURE_2D, this.atlasTexture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE,
          this.atlasCanvas);
        this.needsRender = true;
      });
    }

    setMarkers(items, selected) {
      if (!this.supported) return;
      this.markers = items || [];
      if (selected !== undefined) this.selected = selected;
      this.markerCount = this.markers.length;
      this._buildMarkerInstances();
      if (!this._userMovedCamera) this.resetCamera();
      this.needsRender = true;
    }

    setSelected(gid) {
      this.selected = gid;
      this._buildMarkerInstances();
      this.needsRender = true;
    }

    _buildMarkerInstances() {
      const gl = this.gl;
      const size = 0.62;
      const data = new Float32Array(this.markers.length * 6);
      this.markers.forEach((marker, index) => {
        const o = index * 6;
        data[o] = marker.x + 0.5;
        data[o + 1] = marker.y + 0.5;
        data[o + 2] = marker.z + 0.5;
        data[o + 3] = size;
        data[o + 4] = size;
        data[o + 5] = size;
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, this.markerBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    }

    updateMarker(gid, position) {
      const marker = this.markers.find((item) => item.gid === gid);
      if (!marker) return;
      marker.y = position.y;
      marker.z = position.side;
      marker.side = position.side;
      this._buildMarkerInstances();
      this.needsRender = true;
    }

    // ── 相机 ───────────────────────────────────────────────
    _basis() {
      const camera = this.camera;
      const cp = Math.cos(camera.pitch);
      const dir = [
        cp * Math.sin(camera.yaw),
        Math.sin(camera.pitch),
        cp * Math.cos(camera.yaw),
      ];
      const eye = [
        camera.target[0] + dir[0] * camera.distance,
        camera.target[1] + dir[1] * camera.distance,
        camera.target[2] + dir[2] * camera.distance,
      ];
      let fx = -dir[0], fy = -dir[1], fz = -dir[2];
      const flen = Math.hypot(fx, fy, fz) || 1;
      fx /= flen; fy /= flen; fz /= flen;
      // right = normalize(cross(forward, worldUp))
      let rx = -fz;
      const ry = 0;
      const rz = fx;
      const rlen = Math.hypot(rx, ry, rz) || 1;
      rx /= rlen;
      const ryn = ry / rlen;
      const rzn = rz / rlen;
      const ux = ryn * fz - rzn * fy;
      const uy = rzn * fx - rx * fz;
      const uz = rx * fy - ryn * fx;
      return { eye, dir, right: [rx, ryn, rzn], up: [ux, uy, uz] };
    }

    _viewProj() {
      const { eye } = this._basis();
      const camera = this.camera;
      const width = this.canvas.width || 1;
      const height = this.canvas.height || 1;
      const radius = Math.max(
        Math.hypot(this.bounds.size[0], this.bounds.size[1], this.bounds.size[2]) / 2, 1);
      const near = Math.max(0.2, camera.distance * 0.01);
      const far = camera.distance * 6 + radius * 10;
      const proj = perspective(50 * Math.PI / 180, width / height, near, far);
      const view = lookAt(eye, camera.target, [0, 1, 0]);
      return multiply(proj, view);
    }

    resetCamera() {
      const size = this.bounds.size || [1, 1, 1];
      const min = this.bounds.min || [0, 0, 0];
      const max = this.bounds.max || [0, 0, 0];
      this.camera.target = [
        (min[0] + max[0]) / 2,
        (min[1] + max[1]) / 2,
        (min[2] + max[2]) / 2,
      ];
      if (this.options.enabled !== false && this.markers.length) {
        // 编辑器：把镜头对准轨道开头，保证起始标记清晰可见
        const ys = this.markers.map((item) => item.y);
        const zs = this.markers.map((item) => item.z);
        const xs = this.markers.map((item) => item.x);
        const midY = (Math.min(...ys) + Math.max(...ys)) / 2;
        const midZ = (Math.min(...zs) + Math.max(...zs)) / 2;
        const spread = Math.max(Math.max(...ys) - Math.min(...ys),
          Math.max(...zs) - Math.min(...zs));
        this.camera.target = [Math.max(...xs) + 14, midY + 0.5, midZ + 0.5];
        this.camera.distance = Math.min(180, Math.max(38, spread * 1.5 + 22));
      } else {
        // 只读预览：长轨道不再无限拉远，先给出可辨认方块的取景，滚轮仍可看全貌
        const radius = Math.max(Math.hypot(size[0], size[1], size[2]) / 2, 2);
        const fit = radius * 2.05 + 6;
        const cross = Math.max(size[1], size[2]) * 2.05 + 12;
        this.camera.distance = Math.min(fit, Math.max(cross, 96));
      }
      this.camera.yaw = -0.72;
      this.camera.pitch = 0.52;
      this._userMovedCamera = false;
      this.needsRender = true;
    }

    setView(mode) {
      const camera = this.camera;
      if (mode === 'top') { camera.yaw = 0; camera.pitch = 1.44; }
      if (mode === 'front') { camera.yaw = Math.PI; camera.pitch = 0.12; }
      if (mode === 'side') { camera.yaw = -Math.PI / 2; camera.pitch = 0.12; }
      if (mode === 'iso') { camera.yaw = -0.72; camera.pitch = 0.52; }
      this._userMovedCamera = true;
      this.needsRender = true;
    }

    focusMarker(gid) {
      const marker = this.markers.find((item) => item.gid === gid);
      if (!marker) return;
      this.camera.target = [marker.x + 0.5, marker.y + 0.5, marker.z + 0.5];
      this.camera.distance = Math.min(this.camera.distance, 26);
      this.selected = gid;
      this._buildMarkerInstances();
      this.needsRender = true;
    }

    setSpin(value) {
      this.spin = !!value;
      this.needsRender = true;
    }

    // ── 尺寸与渲染 ─────────────────────────────────────────
    resize() {
      if (!this.supported) return;
      const rect = this.wrap.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.floor(rect.width * dpr));
      const height = Math.max(1, Math.floor(rect.height * dpr));
      if (this.canvas.width !== width || this.canvas.height !== height) {
        this.canvas.width = width;
        this.canvas.height = height;
        this.needsRender = true;
      }
    }

    requestRender() { this.needsRender = true; }

    dispose() {
      cancelAnimationFrame(this.__raf);
      if (!this.supported) return;
      const gl = this.gl;
      gl.deleteBuffer(this.instanceBuffer);
      gl.deleteBuffer(this.markerBuffer);
      gl.deleteBuffer(this.indexBuffer);
      gl.deleteTexture(this.atlasTexture);
      gl.deleteProgram(this.cubeProgram);
      gl.deleteProgram(this.markerProgram);
    }

    _loop(now) {
      this.__raf = requestAnimationFrame(this._loop);
      const dt = Math.min(0.05, (now - this.lastFrame) / 1000);
      this.lastFrame = now;
      if (document.hidden) return;
      if (this.spin) {
        this.camera.yaw += dt * 0.32;
        this.needsRender = true;
      }
      if (!this.needsRender) return;
      this.needsRender = false;
      this.resize();
      this._render();
    }

    _render() {
      const gl = this.gl;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const vp = this._viewProj();

      if (this.blockCount) {
        gl.useProgram(this.cubeProgram);
        gl.uniformMatrix4fv(this.uniforms.viewProj, false, vp);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.atlasTexture);
        gl.uniform1i(this.uniforms.atlas, 0);
        gl.uniform1f(this.uniforms.cols, COLS);
        gl.uniform3f(this.uniforms.light, 0.42, 0.86, 0.31);
        gl.bindVertexArray(this.vao);
        gl.drawElementsInstanced(gl.TRIANGLES, 36, gl.UNSIGNED_SHORT, 0,
          this.blockCount);
      }

      if (this.markerCount) {
        gl.useProgram(this.markerProgram);
        gl.uniformMatrix4fv(this.markerUniforms.viewProj, false, vp);
        gl.bindVertexArray(this.markerVao);
        this.markers.forEach((marker, index) => {
          const active = this.selected === marker.gid;
          const offset = index * 24;
          gl.vertexAttribPointer(3, 3, gl.FLOAT, false, 24, offset);
          gl.vertexAttribPointer(5, 3, gl.FLOAT, false, 24, offset + 12);
          gl.uniform4f(this.markerUniforms.color,
            1, active ? 0.88 : 0.58, active ? 0.42 : 0.26, active ? 0.99 : 0.92);
          gl.drawElementsInstanced(gl.TRIANGLES, 36, gl.UNSIGNED_SHORT, 0, 1);
        });
      }
      gl.bindVertexArray(null);
    }

    _worldPerPixel() {
      return 2 * this.camera.distance * Math.tan(25 * Math.PI / 180)
        / Math.max(1, this.canvas.clientHeight);
    }

    // ── 交互 ───────────────────────────────────────────────
    _bindEvents() {
      const canvas = this.canvas;
      canvas.addEventListener('contextmenu', (event) => event.preventDefault());
      // 阻止鼠标中键触发浏览器的自动滚动 / 翻页
      canvas.addEventListener('mousedown', (event) => {
        if (event.button === 1) event.preventDefault();
      });
      canvas.addEventListener('auxclick', (event) => {
        if (event.button === 1) event.preventDefault();
      });
      canvas.addEventListener('wheel', (event) => {
        event.preventDefault();
        this._userMovedCamera = true;
        const factor = Math.exp((event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY) * 0.0009);
        this.camera.distance = Math.min(2000, Math.max(2,
          this.camera.distance * factor));
        this.needsRender = true;
      }, { passive: false });

      canvas.addEventListener('pointerdown', (event) => this._pointerDown(event));
      canvas.addEventListener('pointermove', (event) => this._pointerMove(event));
      canvas.addEventListener('pointerup', (event) => this._pointerUp(event));
      canvas.addEventListener('pointercancel', (event) => this._pointerUp(event));
      canvas.addEventListener('dblclick', () => this.resetCamera());

      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(this.wrap);
    }

    _markerAt(event) {
      if (!this.markers.length) return null;
      const rect = this.canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const vp = this._viewProj();
      let best = null;
      let bestDistance = 30;
      for (const marker of this.markers) {
        const world = [marker.x + 0.5, marker.y + 0.5, marker.z + 0.5, 1];
        const clip = [
          vp[0] * world[0] + vp[4] * world[1] + vp[8] * world[2] + vp[12],
          vp[1] * world[0] + vp[5] * world[1] + vp[9] * world[2] + vp[13],
          vp[2] * world[0] + vp[6] * world[1] + vp[10] * world[2] + vp[14],
          vp[3] * world[0] + vp[7] * world[1] + vp[11] * world[2] + vp[15],
        ];
        if (clip[3] <= 0.001) continue;
        const sx = (clip[0] / clip[3] * 0.5 + 0.5) * rect.width;
        const sy = (0.5 - clip[1] / clip[3] * 0.5) * rect.height;
        const distance = Math.hypot(sx - px, sy - py);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = marker;
        }
      }
      return best;
    }

    _pointerDown(event) {
      this.canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      this._userMovedCamera = true;
      if (this.pointers.size === 2) {
        this.drag = null;
        this.mode = 'pinch';
        const points = [...this.pointers.values()];
        this.pinch = {
          distance: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y),
          center: [(points[0].x + points[1].x) / 2, (points[0].y + points[1].y) / 2],
          cameraDistance: this.camera.distance,
        };
        return;
      }
      const editable = this.options.enabled !== false && this.markers.length;
      const marker = editable ? this._markerAt(event) : null;
      if (marker && event.button === 0) {
        this.mode = 'marker';
        this.selected = marker.gid;
        this._buildMarkerInstances();
        this.drag = {
          gid: marker.gid,
          x: event.clientX,
          y: event.clientY,
          base: { y: marker.y, side: marker.side },
          moved: false,
        };
        this.wrap.classList.add('is-dragging-marker');
        this.options.onSelect && this.options.onSelect(marker.gid);
        this.needsRender = true;
        return;
      }
      this.mode = event.button === 0 && !event.shiftKey ? 'rotate' : 'pan';
      this.last = { x: event.clientX, y: event.clientY };
    }

    _pointerMove(event) {
      if (!this.pointers.has(event.pointerId)) return;
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

      if (this.mode === 'pinch' && this.pointers.size >= 2 && this.pinch) {
        const points = [...this.pointers.values()];
        const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
        const center = [(points[0].x + points[1].x) / 2, (points[0].y + points[1].y) / 2];
        if (distance > 1 && this.pinch.distance > 1) {
          this.camera.distance = Math.min(2000, Math.max(2,
            this.pinch.cameraDistance * (this.pinch.distance / distance)));
        }
        this._pan(center[0] - this.pinch.center[0], center[1] - this.pinch.center[1]);
        this.pinch.center = center;
        this.pinch.distance = distance;
        this.needsRender = true;
        return;
      }

      if (this.mode === 'marker' && this.drag) {
        const k = this._worldPerPixel();
        const dx = event.clientX - this.drag.x;
        const dy = event.clientY - this.drag.y;
        const { right, up } = this._basis();
        const wx = right[0] * dx * k + up[0] * -dy * k;
        const wy = right[1] * dx * k + up[1] * -dy * k;
        const wz = right[2] * dx * k + up[2] * -dy * k;
        const y = Math.max(-24, Math.min(96, Math.round(this.drag.base.y + wy)));
        const side = Math.max(-128, Math.min(128, Math.round(this.drag.base.side + wz)));
        if (y !== this.drag.lastY || side !== this.drag.lastSide) {
          this.drag.lastY = y;
          this.drag.lastSide = side;
          this.drag.moved = true;
          this.updateMarker(this.drag.gid, { y, side });
          this.options.onMarkerMove && this.options.onMarkerMove(
            this.drag.gid, { y, side }, false);
        }
        void wx;
        return;
      }

      const last = this.last || { x: event.clientX, y: event.clientY };
      const dx = event.clientX - last.x;
      const dy = event.clientY - last.y;
      this.last = { x: event.clientX, y: event.clientY };

      if (this.mode === 'rotate') {
        this.camera.yaw -= dx * 0.0052;
        this.camera.pitch = Math.max(-1.45, Math.min(1.45,
          this.camera.pitch + dy * 0.0046));
        this.needsRender = true;
      } else if (this.mode === 'pan') {
        this._pan(dx * 0.85, dy * 0.85);
        this.needsRender = true;
      }
    }

    _pan(dx, dy) {
      const k = this._worldPerPixel();
      const { right, up } = this._basis();
      const t = this.camera.target;
      t[0] -= right[0] * dx * k - up[0] * dy * k;
      t[1] -= right[1] * dx * k - up[1] * dy * k;
      t[2] -= right[2] * dx * k - up[2] * dy * k;
      this.needsRender = true;
    }

    _pointerUp(event) {
      this.pointers.delete(event.pointerId);
      if (this.mode === 'marker' && this.drag) {
        if (this.drag.moved) {
          this.options.onMarkerMove && this.options.onMarkerMove(
            this.drag.gid, { y: this.drag.lastY, side: this.drag.lastSide }, true);
        }
        this.drag = null;
        this.wrap.classList.remove('is-dragging-marker');
      }
      if (this.pointers.size === 0) {
        this.mode = null;
        this.pinch = null;
      }
      if (this.pointers.size === 1) {
        this.mode = 'rotate';
        const point = [...this.pointers.values()][0];
        this.last = { x: point.x, y: point.y };
      }
    }
  }

  // 缺纹理时用稳定颜色填充
  function _colorTile(block) {
    const canvas = document.createElement('canvas');
    canvas.width = 16;
    canvas.height = 16;
    const ctx = canvas.getContext('2d');
    let hash = 0;
    const text = String(block);
    for (let i = 0; i < text.length; i++) {
      hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }
    const r = 70 + (hash & 0x7f);
    const g = 70 + ((hash >> 8) & 0x7f);
    const b = 70 + ((hash >> 16) & 0x7f);
    ctx.fillStyle = `rgb(${r},${g},${b})`;
    ctx.fillRect(0, 0, 16, 16);
    ctx.fillStyle = `rgba(0,0,0,.18)`;
    ctx.fillRect(0, 0, 16, 1);
    ctx.fillRect(0, 15, 16, 1);
    ctx.fillRect(0, 0, 1, 16);
    ctx.fillRect(15, 0, 1, 16);
    return canvas;
  }

  window.MiauViewer = MiauViewer;
})();
