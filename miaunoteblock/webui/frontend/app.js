/* MiauRedstoneMusic · 网页工作台交互层 */
(function () {
  'use strict';

  const tokenMeta = document.querySelector('meta[name="miau-token"]');
  const TOKEN = tokenMeta ? tokenMeta.content : '';

  const STEPS = [
    { n: 1, name: '歌曲', sub: '载入 .nbs' },
    { n: 2, name: '音轨', sub: '分组与主音轨' },
    { n: 3, name: '排版', sub: '结构形态' },
    { n: 4, name: '方块', sub: '自定义材质' },
    { n: 5, name: '预览', sub: '3D 检查' },
    { n: 6, name: '编辑', sub: '轨道起点' },
    { n: 7, name: '导出', sub: '.schem' },
  ];

  const app = {
    state: null,
    step: 1,
    preview: null,
    previewViewer: null,
    editorViewer: null,
    editor2d: null,
    editorMode: '3d',
    selectedVid: null,
    previewToken: 0,
    dirty: true,
    previewTicks: 240,
    generated: null,
    textureCache: new Map(),
    infoCache: new Map(),
    patchTimer: 0,
    offsetTimer: 0,
  };

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  // ── 基础请求 ─────────────────────────────────────────────
  async function api(path, options) {
    const opts = options || {};
    const init = {
      method: opts.body ? 'POST' : 'GET',
      headers: { 'X-Miau-Token': TOKEN },
      cache: 'no-store',
    };
    if (opts.body) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new Error('无法连接本地服务，请确认启动窗口仍然开着');
    }
    let data;
    try {
      data = await response.json();
    } catch (err) {
      throw new Error('服务返回了无法解析的内容');
    }
    if (!data.ok) throw new Error(data.error || '请求失败');
    return data;
  }

  function toast(message, kind) {
    const host = $('#toasts');
    const item = document.createElement('div');
    item.className = 'toast' + (kind ? ' is-' + kind : ' is-info');
    item.textContent = message;
    host.appendChild(item);
    setTimeout(() => {
      item.style.transition = 'opacity .25s ease, transform .25s ease';
      item.style.opacity = '0';
      item.style.transform = 'translateY(6px)';
      setTimeout(() => item.remove(), 280);
    }, kind === 'error' ? 5200 : 2800);
  }

  function debounce(fn, delay) {
    let timer = 0;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(null, args), delay);
    };
  }

  async function textureSrc(block) {
    if (app.textureCache.has(block)) return app.textureCache.get(block);
    const response = await fetch('/api/texture?block=' + encodeURIComponent(block), {
      headers: { 'X-Miau-Token': TOKEN },
    });
    if (!response.ok) throw new Error('texture');
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    app.textureCache.set(block, url);
    return url;
  }

  function applyTexture(image, block) {
    if (!image) return;
    textureSrc(block).then((url) => { image.src = url; }).catch(() => {});
  }

  // ── 导航 ─────────────────────────────────────────────────
  function renderRail() {
    const list = $('#rail-list');
    list.innerHTML = STEPS.map((step) => (
      '<li class="rail-item">' +
      '<button class="rail-btn" type="button" data-step="' + step.n + '">' +
      '<span class="rail-num"><span>' + String(step.n).padStart(2, '0') + '</span></span>' +
      '<span class="rail-label"><span class="rail-name">' + step.name + '</span>' +
      '<span class="rail-sub">' + step.sub + '</span></span>' +
      '</button></li>'
    )).join('');
    list.addEventListener('click', (event) => {
      const button = event.target.closest('.rail-btn');
      if (button) go(Number(button.dataset.step));
    });
  }

  function songLoaded() {
    return !!(app.state && app.state.song);
  }

  function go(n) {
    if (typeof closePicker === 'function') closePicker();
    if (n > 1 && !songLoaded()) {
      toast('请先载入一首 NBS 歌曲', 'error');
      return;
    }
    app.step = n;
    $$('.step').forEach((panel) => {
      panel.classList.toggle('is-active', Number(panel.dataset.step) === n);
    });
    $$('.rail-btn').forEach((button) => {
      const value = Number(button.dataset.step);
      button.classList.toggle('is-active', value === n);
      button.classList.toggle('is-done', value < n && (value === 1 ? songLoaded() : true));
      button.disabled = value > 1 && !songLoaded();
    });
    $('#rail-count').textContent = String(n).padStart(2, '0') + ' / 7';
    $('#rail-bar').style.width = Math.round((n / 7) * 100) + '%';

    if (n === 5 || n === 6) {
      mountPreviewViewer();
      ensurePreview(false).then(() => {
        if (app.previewViewer) app.previewViewer.resize();
        if (app.editorViewer) app.editorViewer.resize();
      });
    }
    if (n === 6) mountEditor();
    if (n === 7) renderExportSummary();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  document.addEventListener('click', (event) => {
    const goto = event.target.closest('[data-goto]');
    if (goto) go(Number(goto.dataset.goto));
  });

  // ── 状态渲染 ─────────────────────────────────────────────
  function applyState(state) {
    app.state = state;
    const song = state.song;
    const chipSong = $('#chip-song');

    if (song) {
      $('#chip-song-text').textContent = song.name + ' · ' + song.notes + ' 音符';
      chipSong.className = 'chip is-ok';
      $('#song-card').hidden = false;
      $('#song-empty').hidden = true;
      $('#song-name').textContent = song.name;
      $('#song-author').textContent = song.author;
      $('#song-path').value = song.path;
      $('#song-stats').innerHTML = [
        ['音符', String(song.notes)],
        ['原始音轨', String(song.layers)],
        ['分组', String(state.groups.length)],
        ['长度', song.seconds + ' 秒'],
      ].map((pair) => '<div><dt>' + pair[0] + '</dt><dd>' + pair[1] + '</dd></div>').join('');
      const slider = $('#preview-ticks');
      slider.max = Math.max(16, song.maxTick);
      if (!app.previewTicksTouched) {
        app.previewTicks = Math.min(song.maxTick, 240);
        slider.value = app.previewTicks;
      }
      $('#preview-ticks-label').textContent = app.previewTicks + ' tick';
      $('#preview-badge').textContent = song.maxTick + ' tick 全曲';
      const exportTicks = $('#export-ticks');
      if (exportTicks && document.activeElement !== exportTicks) {
        exportTicks.max = Math.max(1, song.maxTick);
        exportTicks.placeholder = String(song.maxTick);
      }
      $('#export-ticks-hint').textContent = 'tick（留空 = 全曲 ' + song.maxTick + '）';
    } else {
      $('#chip-song-text').textContent = '尚未载入歌曲';
      chipSong.className = 'chip chip-muted';
      $('#song-card').hidden = true;
      $('#song-empty').hidden = false;
    }

    renderTracks();
    renderStyles();
    renderParams();
    renderParticles();
    renderBlocks();
    if (!app.generated) $('#output-path').value = $('#output-path').value || state.defaultOutput;
    if (song && app.preview) {
      // 配置变化后预览标记可能失效，保留旧画面直到重新加载
      $('#preview-warnings').innerHTML = '';
    }
    updateRailState();
  }

  function updateRailState() {
    $$('.rail-btn').forEach((button) => {
      const value = Number(button.dataset.step);
      button.disabled = value > 1 && !songLoaded();
      button.classList.toggle('is-active', value === app.step);
    });
  }

  // ── 音轨 ─────────────────────────────────────────────────
  function renderTracks() {
    const board = $('#group-board');
    if (!app.state || !app.state.song) {
      board.innerHTML = '';
      $('#track-summary').textContent = '载入歌曲后显示音轨。';
      $('#group-count').textContent = '0 组';
      $('#track-detail-card').hidden = true;
      return;
    }
    const tracks = app.state.tracks;
    const groups = app.state.groups;
    const byVid = new Map(tracks.map((track) => [track.vid, track]));

    if (app.selectedVid == null || !byVid.has(app.selectedVid)) {
      app.selectedVid = tracks.length ? tracks[0].vid : null;
    }
    const selected = app.selectedVid == null ? null : byVid.get(app.selectedVid);

    board.innerHTML = groups.map((group) => {
      const members = group.vids.map((vid) => byVid.get(vid)).filter(Boolean);
      const chips = members.map((track) => (
        '<button class="track-chip' +
        (track.vid === app.selectedVid ? ' is-selected' : '') +
        (track.master ? ' is-master' : '') +
        '" type="button" draggable="true" data-vid="' + track.vid + '">' +
        '<span class="track-chip-dot"></span>' +
        '<span class="track-chip-body">' +
        '<strong>' + escapeHtml(track.name) + '</strong>' +
        '<span>' + escapeHtml(track.category || '普通音轨') + ' · ' + track.notes + ' 音符</span>' +
        '</span>' +
        (track.master ? '<span class="track-badge">主</span>' : '') +
        (track.stereo ? '<span class="track-badge is-stereo">立体</span>' : '') +
        '</button>'
      )).join('');

      const takeButton = selected && selected.group !== group.gid
        ? '<button class="group-take" type="button" data-take="' + group.gid + '">移入本组</button>'
        : '';

      return '<section class="group-card" data-gid="' + group.gid + '">' +
        '<header class="group-head">' +
        '<span class="group-index">组 ' + (group.gid + 1) + '</span>' +
        '<span class="group-meta">' + members.length + ' 条 · ' + group.noteCount + ' 音符</span>' +
        takeButton +
        '</header>' +
        '<div class="group-tracks">' + chips + '</div>' +
        '</section>';
    }).join('') +
      '<button class="group-new" id="group-new" type="button">' +
      '+ 新建分组</button>';

    $('#track-summary').textContent = tracks.length + ' 条虚拟音轨 · ' +
      groups.length + ' 个分组 · 同组共用一条红石轨道起点';
    $('#group-count').textContent = groups.length + ' 组';

    bindGroupBoard();
    renderTrackDetail();
  }

  function bindGroupBoard() {
    $$('#group-board [data-vid]').forEach((chip) => {
      const vid = Number(chip.dataset.vid);
      chip.addEventListener('click', () => {
        app.selectedVid = vid;
        renderTracks();
      });
      chip.addEventListener('dragstart', (event) => {
        app.selectedVid = vid;
        event.dataTransfer.setData('text/plain', String(vid));
        event.dataTransfer.effectAllowed = 'move';
        chip.classList.add('is-dragging');
      });
      chip.addEventListener('dragend', () => chip.classList.remove('is-dragging'));
    });

    $$('#group-board .group-card').forEach((card) => {
      const gid = Number(card.dataset.gid);
      card.addEventListener('dragover', (event) => {
        event.preventDefault();
        card.classList.add('is-over');
      });
      card.addEventListener('dragleave', () => card.classList.remove('is-over'));
      card.addEventListener('drop', (event) => {
        event.preventDefault();
        card.classList.remove('is-over');
        const vid = Number(event.dataTransfer.getData('text/plain'));
        if (!Number.isNaN(vid)) moveTrackToGroup(vid, gid);
      });
    });

    $$('#group-board [data-take]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        if (app.selectedVid != null) moveTrackToGroup(app.selectedVid, Number(button.dataset.take));
      });
    });

    const newGroup = $('#group-new');
    if (newGroup) {
      newGroup.addEventListener('click', () => {
        if (app.selectedVid == null) return;
        const maxGid = Math.max(0, ...app.state.groups.map((group) => group.gid));
        moveTrackToGroup(app.selectedVid, maxGid + 1);
      });
    }
  }

  function renderTrackDetail() {
    const card = $('#track-detail-card');
    if (!app.state || !app.state.song || app.selectedVid == null) {
      card.hidden = true;
      return;
    }
    const track = app.state.tracks.find((item) => item.vid === app.selectedVid);
    if (!track) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $('#track-detail-title').textContent = track.name;
    $('#track-detail-meta').textContent = (track.category || '普通音轨') + ' · 原音轨 ID ' +
      track.id + ' · ' + track.notes + ' 音符 · 力度 ' + Math.round(track.volume) + '%';
    $('#track-stereo').checked = !!track.stereo;
    $('#track-master').checked = !!track.master;
  }

  async function moveTrackToGroup(vid, targetGid) {
    const groups = app.state.groups.map((group) => group.vids.slice());
    let moving = null;
    groups.forEach((vids) => {
      const index = vids.indexOf(vid);
      if (index >= 0) {
        vids.splice(index, 1);
        moving = vid;
      }
    });
    if (moving == null) return;
    const current = app.state.tracks.find((track) => track.vid === vid);
    if (current && current.group === targetGid) return;
    while (groups.length <= targetGid) groups.push([]);
    groups[targetGid].push(vid);
    const compact = groups.filter((vids) => vids.length);
    try {
      const data = await api('/api/groups', { body: { groups: compact } });
      app.selectedVid = vid;
      app.dirty = true;
      applyState(data.state);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function moveSelectedToOwnGroup() {
    if (app.selectedVid == null) return;
    const maxGid = Math.max(0, ...app.state.groups.map((group) => group.gid));
    await moveTrackToGroup(app.selectedVid, maxGid + 1);
    toast('已拆成独立分组', 'ok');
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function onStereoChange(event) {
    const track = app.state.tracks.find((item) => item.vid === app.selectedVid);
    if (!track) return;
    const ids = new Set(app.state.tracks.filter((item) => item.stereo).map((item) => item.id));
    if (event.target.checked) ids.add(track.id); else ids.delete(track.id);
    try {
      const data = await api('/api/config', {
        body: { patch: { stereo_layers: Array.from(ids).sort((a, b) => a - b) } },
      });
      app.dirty = true;
      applyState(data.state);
    } catch (err) {
      toast(err.message, 'error');
      renderTracks();
    }
  }

  async function onMasterChange(event) {
    const track = app.state.tracks.find((item) => item.vid === app.selectedVid);
    if (!track) return;
    const ids = new Set(app.state.config.curve_master_layers || []);
    if (event.target.checked) ids.add(track.id); else ids.delete(track.id);
    try {
      const data = await api('/api/config', {
        body: { patch: { curve_master_layers: Array.from(ids).sort((a, b) => a - b) } },
      });
      app.dirty = true;
      applyState(data.state);
    } catch (err) {
      toast(err.message, 'error');
      renderTracks();
    }
  }

  // ── 排版 ─────────────────────────────────────────────────
  function renderStyles() {
    const grid = $('#style-grid');
    if (!app.state) return;
    const current = app.state.config.layout_style;
    grid.innerHTML = app.state.styles.map((style) => (
      '<button class="style-card' + (style.id === current ? ' is-active' : '') +
      '" type="button" data-style="' + style.id + '">' +
      '<span class="style-top"><span class="style-name">' + style.name + '</span>' +
      '<span class="style-tag">' + style.tag + '</span></span>' +
      '<span class="style-desc">' + style.desc + '</span>' +
      '<svg class="style-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round"><path d="M4 10.5l4 4 8-9"/></svg>' +
      '</button>'
    )).join('');
    $$('#style-grid .style-card').forEach((card) => {
      card.addEventListener('click', () => {
        if (card.dataset.style === app.state.config.layout_style) return;
        patchConfig({ layout_style: card.dataset.style });
      });
    });
  }

  function renderParams() {
    const grid = $('#param-grid');
    if (!app.state || !app.state.song) {
      grid.innerHTML = '';
      $('#param-desc').textContent = '载入歌曲后可调整排版参数。';
      $('#param-style-name').textContent = '—';
      return;
    }
    const state = app.state;
    const style = state.config.layout_style;
    const meta = state.styles.find((item) => item.id === style);
    const keys = state.layoutParams[style] || [];
    $('#param-desc').textContent = meta ? meta.desc : '';
    $('#param-style-name').textContent = meta ? meta.name : style;
    $('#curve-notice').hidden = style !== 'curve';

    const html = [];
    keys.forEach((key) => {
      if (key === 'curve_master_layers') {
        const count = (state.config.curve_master_layers || []).length;
        html.push(
          '<div class="param param-toggle"><div><span class="param-name">主音轨</span>' +
          '<p>在步骤 2 勾选；固定 100% 音量，排在走廊上下。</p></div>' +
          '<span class="param-value">' + count + ' 条</span></div>');
        return;
      }
      if (key === 'curve_minecart') {
        html.push(
          '<div class="param param-toggle"><div><span class="param-name">隐形矿车跟随</span>' +
          '<p>生成命令方块链，自动召唤、骑乘并逐 tick 传送。</p></div>' +
          '<label class="switch"><input type="checkbox" data-toggle="curve_minecart"' +
          (state.config.curve_minecart ? ' checked' : '') + ' /><span></span></label></div>');
        return;
      }
      if (key === 'curve_trim_silent') {
        html.push(
          '<div class="param param-toggle"><div><span class="param-name">实验性 · 静音裁剪</span>' +
          '<p>删除开头/中间/结尾超过阈值的空音轨段；开头与中间用线路桥在正确时刻唤醒。</p></div>' +
          '<label class="switch"><input type="checkbox" data-toggle="curve_trim_silent"' +
          (state.config.curve_trim_silent ? ' checked' : '') + ' /><span></span></label></div>');
        return;
      }
      if (key === 'curve_dual_track') {
        html.push(
          '<div class="param param-toggle"><div><span class="param-name">实验性 · 双轨音量</span>' +
          '<p>按音量公式算出直线距离；当高低音量的距离差超过 16 格（红石线 15 格 + 音符盒 1 格的物理极限）时，同侧外扩生成副音轨。</p></div>' +
          '<label class="switch"><input type="checkbox" data-toggle="curve_dual_track"' +
          (state.config.curve_dual_track ? ' checked' : '') + ' /><span></span></label></div>');
        return;
      }
      if (key === 'curve_command_side') {
        const sides = [['right', '右'], ['left', '左'], ['up', '上'], ['down', '下']];
        const options = sides.map(([value, name]) =>
          '<option value="' + value + '"' +
          (state.config.curve_command_side === value ? ' selected' : '') +
          '>' + name + '侧</option>');
        html.push(
          '<div class="param"><div class="param-head"><span class="param-name">命令方块链方位</span>' +
          '<span class="param-value">尽量在 48 格外</span></div>' +
          '<select class="input" data-select-text="curve_command_side">' +
          options.join('') + '</select></div>');
        return;
      }
      if (key === 'master_group') {
        const options = ['<option value="">无核心组</option>'].concat(
          state.groups.map((group) => '<option value="' + group.gid + '"' +
            (state.config.master_group === group.gid ? ' selected' : '') +
            '>组 ' + (group.gid + 1) + '</option>'));
        html.push(
          '<div class="param"><div class="param-head"><span class="param-name">核心组</span>' +
          '<span class="param-value">居中</span></div>' +
          '<select class="input" data-select="master_group">' + options.join('') + '</select></div>');
        return;
      }
      const spec = state.paramSpecs[key];
      if (!spec) return;
      const value = state.config[key];
      const step = spec.type === 'float' ? 0.1 : 1;
      html.push(
        '<div class="param"><div class="param-head">' +
        '<span class="param-name">' + spec.label + '</span>' +
        '<span class="param-value" id="pv-' + key + '">' + formatNumber(value, spec) + '</span>' +
        '</div><input type="range" data-param="' + key + '" min="' + spec.min +
        '" max="' + spec.max + '" step="' + step + '" value="' + value + '" /></div>');
    });
    grid.innerHTML = html.join('');

    $$('#param-grid [data-param]').forEach((input) => {
      const key = input.dataset.param;
      const spec = state.paramSpecs[key];
      input.addEventListener('input', () => {
        $('#pv-' + key).textContent = formatNumber(input.value, spec);
      });
      input.addEventListener('change', () => {
        const value = spec.type === 'float' ? Number(input.value) : parseInt(input.value, 10);
        patchConfig({ [key]: value });
      });
    });
    $$('#param-grid [data-toggle]').forEach((input) => {
      input.addEventListener('change', () => {
        patchConfig({ [input.dataset.toggle]: input.checked });
      });
    });
    $$('#param-grid [data-select]').forEach((select) => {
      select.addEventListener('change', () => {
        const value = select.value === '' ? null : Number(select.value);
        patchConfig({ [select.dataset.select]: value });
      });
    });
    $$('#param-grid [data-select-text]').forEach((select) => {
      select.addEventListener('change', () => {
        patchConfig({ [select.dataset.selectText]: select.value });
      });
    });
  }

  function formatNumber(value, spec) {
    const number = Number(value);
    const text = spec.type === 'float' ? (Math.round(number * 10) / 10).toString() : String(number);
    return spec.unit ? text + ' ' + spec.unit : text;
  }

  async function patchConfig(partial) {
    try {
      const data = await api('/api/config', { body: { patch: partial } });
      app.dirty = true;
      applyState(data.state);
      if (app.step === 5 || app.step === 6) {
        schedulePreviewReload();
      }
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  const schedulePreviewReload = debounce(() => {
    if (app.step === 5 || app.step === 6) ensurePreview(true);
  }, 700);

  const SCHEDULE_PRESETS = {
    light: { label: '轻', trail: 12, height: 2, linger: 20 },
    normal: { label: '中', trail: 24, height: 3, linger: 40 },
    strong: { label: '强', trail: 48, height: 5, linger: 60 },
  };
  const PARTICLE_CURVES = [['arc', '弧线'], ['sine', '正弦'], ['line', '直线']];

  // ── 粒子特效 ─────────────────────────────────────────────
  function renderParticles() {
    if (!app.state) return;
    const config = app.state.config;
    const tracks = app.state.tracks || [];
    const enabled = !!config.particles_enabled;
    const selected = new Set(config.particles_layers || []);

    const toggle = $('#particles-enabled');
    toggle.checked = enabled;
    $('#particles-body').classList.toggle('is-off', !enabled);

    const layers = $('#particle-layers');
    layers.innerHTML = tracks.length
      ? tracks.map((track) => (
        '<button class="layer-pill' + (selected.has(track.vid) ? ' is-on' : '') +
        (enabled ? '' : ' is-disabled') + '" type="button" data-vid="' + track.vid + '">' +
        '<span class="layer-dot"></span>' + escapeHtml(track.name) +
        '<em>' + escapeHtml(track.category || '音轨') + '</em></button>'
      )).join('')
      : '<p class="field-hint">载入歌曲后可选择参与粒子的音轨。</p>';
    $$('#particle-layers [data-vid]').forEach((button) => {
      button.addEventListener('click', () => toggleParticleLayer(Number(button.dataset.vid)));
    });

    $$('#particle-presets [data-particle-preset]').forEach((button) => {
      const key = button.dataset.particlePreset;
      button.classList.toggle('is-active', key === config.particles_preset);
      button.disabled = !enabled;
    });

    const params = [
      ['particles_trail', '拖尾长度', 2, 256, 1, ''],
      ['particles_height', '跳动高度', 0.5, 64, 0.5, ' 格'],
      ['particles_linger', '末音符后停留', -1, 600, 1, ' tick'],
    ];
    $('#particle-params').innerHTML = params.map((item) => (
      '<div class="param"><div class="param-head">' +
      '<span class="param-name">' + item[1] + '</span>' +
      '<span class="param-value" id="pv-' + item[0] + '">' +
      formatNumber(config[item[0]], { type: item[4] < 1 ? 'float' : 'int', unit: item[5].trim() }) +
      '</span></div>' +
      '<input type="range" data-particle-param="' + item[0] + '" min="' + item[2] +
      '" max="' + item[3] + '" step="' + item[4] + '" value="' + config[item[0]] +
      '"' + (enabled ? '' : ' disabled') + ' /></div>' +
      (item[0] === 'particles_linger'
        ? '<div class="param param-select"><div class="param-head">' +
          '<span class="param-name">竖直起伏</span></div>' +
          '<select class="input" id="particle-curve"' + (enabled ? '' : ' disabled') + '>' +
          PARTICLE_CURVES.map((pair) => '<option value="' + pair[0] + '"' +
            (config.particles_curve === pair[0] ? ' selected' : '') + '>' + pair[1] +
            '</option>').join('') + '</select></div>'
        : '')
    )).join('');

    $$('#particle-params [data-particle-param]').forEach((input) => {
      const key = input.dataset.particleParam;
      const step = Number(input.step);
      input.addEventListener('input', () => {
        $('#pv-' + key).textContent = formatNumber(input.value, {
          type: step < 1 ? 'float' : 'int',
          unit: key === 'particles_height' ? '格'
            : key === 'particles_linger' ? 'tick' : '',
        });
      });
      input.addEventListener('change', () => {
        const value = step < 1 ? Number(input.value) : parseInt(input.value, 10);
        patchConfig({ [key]: value });
      });
    });
    const curve = $('#particle-curve');
    if (curve) {
      curve.addEventListener('change', () => patchConfig({ particles_curve: curve.value }));
    }

    const color = $('#particle-color');
    const gradient = $('#particle-gradient');
    color.disabled = !enabled;
    gradient.disabled = !enabled;
    if (document.activeElement !== color) color.value = config.particles_color || '';
    if (document.activeElement !== gradient) gradient.value = config.particles_gradient || '';
  }

  async function toggleParticleLayer(vid) {
    if (!app.state) return;
    const selected = new Set(app.state.config.particles_layers || []);
    if (selected.has(vid)) selected.delete(vid); else selected.add(vid);
    patchConfig({ particles_layers: Array.from(selected).sort((a, b) => a - b) });
  }

  function bindParticleControls() {
    $('#particles-enabled').addEventListener('change', (event) => {
      const on = event.target.checked;
      const patch = { particles_enabled: on };
      const current = app.state.config.particles_layers || [];
      if (on && !current.length && app.state.tracks.length) {
        patch.particles_layers = app.state.tracks.map((track) => track.vid);
      }
      patchConfig(patch);
    });

    $$('#particle-presets [data-particle-preset]').forEach((button) => {
      button.addEventListener('click', () => {
        const preset = SCHEDULE_PRESETS[button.dataset.particlePreset];
        if (!preset) return;
        patchConfig({
          particles_preset: button.dataset.particlePreset,
          particles_trail: preset.trail,
          particles_height: preset.height,
          particles_linger: preset.linger,
          particles_enabled: true,
        });
      });
    });

    [['#particle-color', 'particles_color'], ['#particle-gradient', 'particles_gradient']]
      .forEach((pair) => {
        const input = $(pair[0]);
        const commit = () => patchConfig({ [pair[1]]: input.value.trim() });
        input.addEventListener('change', commit);
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') commit();
        });
      });
  }

  // ── 方块 ─────────────────────────────────────────────────
  const BLOCK_KEYS = {
    empty_block: {
      preview: '#preview-empty', status: 'empty',
      label: '空 tick 方块', fallback: '跟随空 tick 方块',
    },
    support_block: {
      preview: '#preview-support', status: 'support',
      label: '支撑方块', fallback: '跟随支撑方块',
    },
    lane_block_left: {
      preview: '#preview-lane-left', status: 'lane-left',
      label: '左音轨空音符方块', fallback: '跟随空 tick 方块',
    },
    lane_support_left: {
      preview: '#preview-support-left', status: 'support-left',
      label: '左音轨支撑方块', fallback: '跟随支撑方块',
    },
    lane_block_right: {
      preview: '#preview-lane-right', status: 'lane-right',
      label: '右音轨空音符方块', fallback: '跟随空 tick 方块',
    },
    lane_support_right: {
      preview: '#preview-support-right', status: 'support-right',
      label: '右音轨支撑方块', fallback: '跟随支撑方块',
    },
  };

  function blockDisplayName(block) {
    const gallery = (app.state && app.state.blockGallery) || [];
    for (let i = 0; i < gallery.length; i++) {
      const found = gallery[i].blocks.find((item) => item.id === block);
      if (found) return found.name;
    }
    return String(block).split(':').pop().replace(/_/g, ' ');
  }

  function renderBlocks() {
    if (!app.state) return;
    const config = app.state.config;
    const fallbackOf = (key) => (key.indexOf('support') >= 0
      ? config.support_block : config.empty_block);
    $$('[data-block-key]').forEach((trigger) => {
      const key = trigger.dataset.blockKey;
      const meta = BLOCK_KEYS[key] || {};
      const value = config[key] || '';
      const nameEl = $('[data-block-name]', trigger);
      const idEl = $('[data-block-id]', trigger);
      if (nameEl) nameEl.textContent = value ? blockDisplayName(value)
        : (meta.fallback || '跟随默认方块');
      if (idEl) idEl.textContent = value || '—';
      const icon = $('.block-trigger-icon img', trigger);
      if (icon) applyTexture(icon, value || fallbackOf(key));
    });
    Object.keys(BLOCK_KEYS).forEach((key) => {
      const meta = BLOCK_KEYS[key];
      const value = config[key] || '';
      setPickerPreview(meta.preview, value || fallbackOf(key));
      checkBlockInfo(meta.status, value);
    });
    const lamp = $('#toggle-master-lamp');
    if (lamp) lamp.checked = !!config.curve_master_lamp;
  }

  function setPickerPreview(selector, block) {
    const host = $(selector);
    if (!host) return;
    let image = $('img', host);
    if (!image) {
      host.innerHTML = '';
      image = document.createElement('img');
      image.alt = '';
      host.appendChild(image);
    }
    applyTexture(image, block);
  }

  // 浮层选择器：一次只打开一个，内容与图标彼此独立，避免互相串改
  const picker = { node: null, key: null, trigger: null, category: 'all', query: '' };

  function ensurePicker() {
    if (picker.node) return picker.node;
    const node = document.createElement('div');
    node.className = 'block-popover';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-label', '选择方块');
    node.hidden = true;
    node.innerHTML =
      '<div class="bp-head">' +
        '<div class="bp-title"><span class="kicker">选择方块</span><strong data-bp-title>空 tick 方块</strong></div>' +
        '<label class="bp-search"><span aria-hidden="true">⌕</span>' +
        '<input type="search" placeholder="搜索名称或 ID" data-bp-search autocomplete="off" /></label>' +
        '<button class="bp-close" type="button" data-bp-close aria-label="关闭">×</button>' +
      '</div>' +
      '<div class="bp-body">' +
        '<nav class="bp-cats" data-bp-cats></nav>' +
        '<div class="bp-grid-wrap">' +
          '<div class="bp-grid" data-bp-grid></div>' +
          '<p class="bp-empty" data-bp-empty hidden>没有匹配的方块</p>' +
        '</div>' +
      '</div>' +
      '<div class="bp-foot">' +
        '<label class="bp-manual"><span>模组方块 ID</span>' +
        '<input class="input input-mono" type="text" spellcheck="false" placeholder="namespace:block_id" data-bp-input /></label>' +
        '<button class="btn btn-ghost btn-sm" type="button" data-bp-reset hidden>跟随空 tick 方块</button>' +
        '<button class="btn btn-primary btn-sm" type="button" data-bp-apply>应用</button>' +
      '</div>';
    document.body.appendChild(node);
    $('[data-bp-close]', node).addEventListener('click', closePicker);
    $('[data-bp-search]', node).addEventListener('input', (event) => {
      picker.query = event.target.value.trim().toLowerCase();
      renderPickerBody();
    });
    $('[data-bp-cats]', node).addEventListener('click', (event) => {
      const chip = event.target.closest('[data-cat]');
      if (!chip) return;
      picker.category = chip.dataset.cat;
      renderPickerBody();
    });
    $('[data-bp-grid]', node).addEventListener('click', (event) => {
      const tile = event.target.closest('[data-block]');
      if (!tile) return;
      commitBlock(tile.dataset.block);
    });
    $('[data-bp-apply]', node).addEventListener('click', () => {
      const value = $('[data-bp-input]', node).value.trim();
      if (value) commitBlock(value);
    });
    $('[data-bp-input]', node).addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const value = event.target.value.trim();
      if (value) commitBlock(value);
    });
    $('[data-bp-reset]', node).addEventListener('click', () => commitBlock(''));
    picker.node = node;
    return node;
  }

  function openPicker(key, trigger) {
    const node = ensurePicker();
    if (picker.trigger && picker.trigger !== trigger) {
      picker.trigger.setAttribute('aria-expanded', 'false');
    }
    picker.key = key;
    picker.trigger = trigger;
    picker.category = 'all';
    picker.query = '';
    const meta = BLOCK_KEYS[key] || {};
    $('[data-bp-title]', node).textContent = meta.label || '选择方块';
    $('[data-bp-search]', node).value = '';
    $('[data-bp-input]', node).value = (app.state && app.state.config[key]) || '';
    const resetButton = $('[data-bp-reset]', node);
    resetButton.hidden = key.indexOf('lane_') !== 0;
    resetButton.textContent = key.indexOf('support') >= 0
      ? '跟随支撑方块' : '跟随空 tick 方块';
    node.hidden = false;
    if (trigger) trigger.setAttribute('aria-expanded', 'true');
    positionPicker(trigger);
    renderPickerBody();
    requestAnimationFrame(() => node.classList.add('is-open'));
    setTimeout(() => $('[data-bp-search]', node).focus(), 30);
  }

  function closePicker() {
    const node = picker.node;
    if (!node) return;
    if (picker.trigger) picker.trigger.setAttribute('aria-expanded', 'false');
    node.classList.remove('is-open');
    setTimeout(() => {
      if (!node.classList.contains('is-open')) node.hidden = true;
    }, 130);
    picker.key = null;
  }

  function positionPicker(trigger) {
    const node = picker.node;
    if (!node || !trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(580, window.innerWidth - 24);
    const height = Math.min(470, window.innerHeight - 24);
    let left = rect.left;
    if (left + width > window.innerWidth - 12) left = window.innerWidth - width - 12;
    left = Math.max(12, left);
    const below = window.innerHeight - rect.bottom;
    let top;
    if (below < 300 && rect.top > below) {
      top = rect.top - height - 10;
      node.dataset.side = 'up';
    } else {
      top = rect.bottom + 10;
      node.dataset.side = 'down';
    }
    top = Math.max(12, Math.min(top, window.innerHeight - height - 12));
    node.style.width = width + 'px';
    node.style.maxHeight = height + 'px';
    node.style.left = left + 'px';
    node.style.top = top + 'px';
  }

  function renderPickerBody() {
    const node = picker.node;
    if (!node || !app.state) return;
    const gallery = app.state.blockGallery || [];
    const total = gallery.reduce((sum, group) => sum + group.blocks.length, 0);
    const chips = ['<button class="bp-cat" type="button" data-cat="all">全部 <em>' + total + '</em></button>']
      .concat(gallery.map((group) => (
        '<button class="bp-cat" type="button" data-cat="' + group.id + '">' +
        escapeHtml(group.name) + ' <em>' + group.blocks.length + '</em></button>'
      )));
    const cats = $('[data-bp-cats]', node);
    cats.innerHTML = chips.join('');
    $$('.bp-cat', cats).forEach((chip) => {
      chip.classList.toggle('is-active', chip.dataset.cat === picker.category);
    });

    const query = picker.query;
    const sections = gallery
      .filter((group) => picker.category === 'all' || group.id === picker.category)
      .map((group) => ({
        group,
        blocks: group.blocks.filter((item) => !query
          || item.name.toLowerCase().indexOf(query) >= 0
          || item.id.toLowerCase().indexOf(query) >= 0),
      }))
      .filter((entry) => entry.blocks.length);

    const active = (app.state.config[picker.key] || '').trim();
    const grid = $('[data-bp-grid]', node);
    grid.innerHTML = sections.map((entry) => (
      '<section class="bp-group"><h4>' + escapeHtml(entry.group.name) +
      '<span>' + entry.blocks.length + '</span></h4><div class="bp-tiles">' +
      entry.blocks.map((item) => (
        '<button class="bp-tile' + (item.id === active ? ' is-active' : '') +
        '" type="button" data-block="' + item.id + '" title="' + item.id + '">' +
        '<img alt="" loading="lazy" /><span>' + escapeHtml(item.name) + '</span></button>'
      )).join('') + '</div></section>'
    )).join('');
    $$('.bp-tile', grid).forEach((tile) => {
      const image = $('img', tile);
      applyTexture(image, tile.dataset.block);
    });
    $('[data-bp-empty]', node).hidden = sections.length > 0;
  }

  async function commitBlock(value) {
    const key = picker.key;
    if (!key) return;
    if (value) {
      const info = await fetchBlockInfo(value);
      if (!info || !info.valid) {
        toast('方块 ID 格式不正确：' + value, 'error');
        return;
      }
      if (info.functional) {
        toast(info.name + ' 会被红石激活，不能作为结构方块', 'error');
        return;
      }
    }
    try {
      const data = await api('/api/config', { body: { patch: { [key]: value } } });
      app.dirty = true;
      applyState(data.state);
      toast(value ? '方块已更新为 ' + blockDisplayName(value) : '已恢复跟随空 tick 方块', 'ok');
      closePicker();
      if (app.step === 5 || app.step === 6) schedulePreviewReload();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function fetchBlockInfo(block) {
    if (app.infoCache.has(block)) return app.infoCache.get(block);
    const promise = fetch('/api/block-info?block=' + encodeURIComponent(block), {
      headers: { 'X-Miau-Token': TOKEN },
    }).then((response) => response.json())
      .then((data) => (data && data.ok ? data.info : null))
      .catch(() => null);
    app.infoCache.set(block, promise);
    return promise;
  }

  async function checkBlockInfo(which, block) {
    const host = $('#status-' + which);
    if (!host) return;
    host.className = 'picker-status';
    if (!block) {
      host.classList.add('is-ok');
      host.textContent = '跟随「空 tick 方块」';
      return;
    }
    host.textContent = '正在检查方块 ID…';
    const info = await fetchBlockInfo(block);
    if (!info) {
      host.textContent = '';
      return;
    }
    if (!info.valid) {
      host.className = 'picker-status is-bad';
      host.textContent = 'ID 格式不正确，应为 命名空间:方块ID（如 minecraft:stone）';
      return;
    }
    if (info.functional) {
      host.className = 'picker-status is-bad';
      host.textContent = '✕ ' + info.name + ' 会被红石激活，不能用作结构方块';
      return;
    }
    if (info.hasTexture) {
      host.className = 'picker-status is-ok';
      host.textContent = '✓ ' + info.name + ' · 已找到原版纹理';
    } else {
      host.className = 'picker-status is-warn';
      host.textContent = '◐ ' + info.name + ' · 资源里没有纹理（模组方块正常），预览使用纯色占位';
    }
  }

  function bindBlockPickers() {
    $$('[data-block-key]').forEach((trigger) => {
      trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        const key = trigger.dataset.blockKey;
        if (picker.key === key) {
          closePicker();
          return;
        }
        openPicker(key, trigger);
      });
    });
    document.addEventListener('click', (event) => {
      if (!picker.node || picker.node.hidden) return;
      if (picker.node.contains(event.target)) return;
      if (event.target.closest && event.target.closest('[data-block-key]')) return;
      closePicker();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && picker.node && !picker.node.hidden) closePicker();
    });
    document.addEventListener('scroll', () => {
      if (picker.node && !picker.node.hidden && picker.trigger) positionPicker(picker.trigger);
    }, true);
    window.addEventListener('resize', () => {
      if (picker.node && !picker.node.hidden && picker.trigger) positionPicker(picker.trigger);
    });
    const lamp = $('#toggle-master-lamp');
    if (lamp) {
      lamp.addEventListener('change', () => patchConfig({ curve_master_lamp: lamp.checked }));
    }
  }

  // ── 预览 ─────────────────────────────────────────────────
  function mountPreviewViewer() {
    if (!app.previewViewer) {
      app.previewViewer = new window.MiauViewer($('#preview-canvas'), { token: TOKEN });
      app.previewViewer.setSpin($('#preview-spin').checked);
    }
    pushPreview(app.previewViewer);
  }

  function mountEditor() {
    if (!app.editorViewer) {
      app.editorViewer = new window.MiauViewer($('#editor-canvas'), {
        token: TOKEN,
        enabled: true,
        onSelect: (gid) => selectMarker(gid),
        onMarkerMove: onMarkerMove,
      });
    }
    if (!app.editor2d) {
      app.editor2d = new window.MiauEditor2D($('#editor2d-canvas'), {
        onSelect: (gid) => selectMarker(gid, false, true),
        onMarkerMove: onMarkerMove,
      });
    }
    const preview = app.preview;
    const editable = preview && preview.markers && preview.markers.editable;
    $('#editor-layout').hidden = !editable;
    $('#editor-locked').hidden = !!editable;
    $('#editor-mode-chip').className = 'chip ' + (editable ? 'is-ok' : 'is-off');
    $('#editor-mode-chip').lastElementChild.textContent = editable ? '可编辑' : '只读';
    if (editable) {
      pushPreview(app.editorViewer);
      app.editorViewer.setMarkers(preview.markers.items, app.editorViewer.selected);
      app.editor2d.setData(preview.bounds, preview.blocks, preview.palette);
      app.editor2d.setMarkers(preview.markers.items, app.editor2d.selected);
      applyEditorMode(app.editorMode);
      renderMarkerList();
      setTimeout(() => {
        app.editorViewer.resize();
        app.editor2d.resize();
      }, 40);
    }
  }

  function applyEditorMode(mode) {
    app.editorMode = mode === '2d' ? '2d' : '3d';
    const is2d = app.editorMode === '2d';
    $('#editor-wrap').hidden = is2d;
    $('#editor2d-wrap').hidden = !is2d;
    $('#editor-3d-tools').hidden = is2d;
    $('#editor-2d-tools').hidden = !is2d;
    $$('[data-editor-mode]').forEach((button) => {
      button.classList.toggle('is-active', button.dataset.editorMode === app.editorMode);
    });
    const markerCount = app.preview && app.preview.markers
      ? app.preview.markers.items.length : 0;
    $('#editor-badge').hidden = is2d;
    $('#editor2d-badge').hidden = !is2d;
    $('#editor-badge').textContent = markerCount + ' 个起始点';
    $('#editor2d-badge').textContent = is2d
      ? 'X-Y 切面 · ' + markerCount + ' 个起始点'
      : markerCount + ' 个起始点';
    if (is2d) app.editor2d.resize();
    else app.editorViewer.resize();
  }

  function pushPreview(viewer) {
    if (!viewer || !app.preview) return;
    viewer.setData(app.preview.palette, app.preview.blocks, app.preview.bounds);
    if (viewer === app.editorViewer && app.preview.markers) {
      viewer.setMarkers(app.preview.markers.items, viewer.selected);
    }
  }

  async function ensurePreview(force, silent) {
    if (!songLoaded()) return;
    if (!force && app.preview && !app.dirty) {
      renderPreviewMeta();
      return;
    }
    const token = ++app.previewToken;
    const loading = $('#preview-loading');
    const editorLoading = $('#editor-loading');
    if (!silent) {
      if (loading) loading.hidden = false;
      if (editorLoading) editorLoading.hidden = false;
    }
    try {
      const data = await api('/api/preview?ticks=' + app.previewTicks);
      if (token !== app.previewToken) return;
      app.preview = data.preview;
      app.dirty = false;
      pushPreview(app.previewViewer);
      if (app.editorViewer) {
        pushPreview(app.editorViewer);
        if (app.editorViewer) app.editorViewer.setMarkers(
          app.preview.markers.items, app.editorViewer.selected);
      }
      if (app.editor2d) {
        app.editor2d.setData(app.preview.bounds, app.preview.blocks, app.preview.palette);
        app.editor2d.setMarkers(app.preview.markers.items, app.editor2d.selected);
      }
      renderPreviewMeta();
      if (app.step === 6) mountEditor();
      else renderMarkerList();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      if (!silent && token === app.previewToken) {
        if (loading) loading.hidden = true;
        if (editorLoading) editorLoading.hidden = true;
      }
    }
  }

  function renderPreviewMeta() {
    const preview = app.preview;
    if (!preview) return;
    const bounds = preview.bounds;
    $('#preview-badge').textContent =
      bounds.size[0] + ' × ' + bounds.size[1] + ' × ' + bounds.size[2];
    if (app.previewViewer) app.previewViewer.resize();

    const stats = [
      ['方块', preview.stats.blocks.toLocaleString('zh-CN')],
      ['音符盒', preview.stats.noteBlocks.toLocaleString('zh-CN')],
      ['音色', preview.stats.voices + ' 种'],
      ['分组', String(preview.stats.groups)],
      ['虚拟音轨', String(preview.stats.tracks)],
      ['尺寸', bounds.size[0] + '×' + bounds.size[1] + '×' + bounds.size[2]],
    ];
    $('#preview-stats').innerHTML = stats.map((pair) => (
      '<div class="stat-tile"><span>' + pair[0] + '</span><strong>' + pair[1] + '</strong>' +
      (pair[0] === '音色' ? '<em class="stat-note">' +
        escapeHtml((preview.stats.voiceNames || []).slice(0, 4).join(' / ')) + '</em>' : '') +
      '</div>'
    )).join('');

    const legend = preview.counts.slice(0, 8);
    $('#preview-legend').innerHTML = legend.map((entry, index) => (
      '<span class="legend-chip" data-legend="' + index + '">' +
      '<img alt="" /><span>' + prettyBlock(entry.block) + '</span>' +
      '<b>' + entry.count + '</b></span>'
    )).join('');
    $$('#preview-legend .legend-chip').forEach((chip, index) => {
      applyTexture($('img', chip), legend[index].block);
    });

    $('#preview-warnings').innerHTML = preview.warnings.map((text) => (
      '<div class="warning-item">' + escapeHtml(text) + '</div>'
    )).join('');
    updateWarningChip(preview.warnings);
    $('#editor-badge').textContent = preview.markers.editable
      ? preview.markers.items.length + ' 个起始点'
      : '只读';
  }

  function updateWarningChip(warnings) {
    const chip = $('#chip-warnings');
    if (!chip) return;
    if (!warnings || !warnings.length) {
      chip.hidden = true;
      chip.title = '';
      return;
    }
    chip.hidden = false;
    $('#chip-warnings-text').textContent = '警告 ' + warnings.length + ' 条';
    chip.title = warnings.join('\n');
  }

  function prettyBlock(block) {
    const id = String(block).split('[')[0].replace('minecraft:', '');
    return id.replace(/_/g, ' ');
  }

  function bindPreviewControls() {
    $$('[data-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!app.previewViewer) return;
        app.previewViewer.setView(button.dataset.view);
        markToolActive(button);
      });
    });
    $$('[data-editor-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!app.editorViewer) return;
        app.editorViewer.setView(button.dataset.editorView);
        markToolActive(button);
      });
    });
    $$('[data-editor-mode]').forEach((button) => {
      button.addEventListener('click', () => applyEditorMode(button.dataset.editorMode));
    });
    $$('[data-edit2d-zoom]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!app.editor2d) return;
        const action = button.dataset.edit2dZoom;
        if (action === 'in') app.editor2d.zoomBy(1.25);
        else if (action === 'out') app.editor2d.zoomBy(1 / 1.25);
        else app.editor2d.fit();
      });
    });
    $('#preview-reset').addEventListener('click', () => {
      if (app.previewViewer) app.previewViewer.resetCamera();
    });
    $('#editor-reset-cam').addEventListener('click', () => {
      if (app.editorMode === '2d') {
        if (app.editor2d) app.editor2d.fit();
      } else if (app.editorViewer) {
        app.editorViewer.resetCamera();
      }
    });
    $('#preview-spin').addEventListener('change', (event) => {
      if (app.previewViewer) app.previewViewer.setSpin(event.target.checked);
    });
    $('#preview-ticks').addEventListener('input', (event) => {
      app.previewTicksTouched = true;
      app.previewTicks = Number(event.target.value);
      $('#preview-ticks-label').textContent = app.previewTicks + ' tick';
    });
    $('#preview-ticks').addEventListener('change', () => {
      app.dirty = true;
      ensurePreview(true);
    });
    $('#preview-refresh').addEventListener('click', () => {
      app.dirty = true;
      ensurePreview(true);
    });
  }

  function markToolActive(button) {
    const group = button.parentElement;
    $$('.tool', group).forEach((item) => item.classList.toggle('is-active', item === button));
  }

  // ── 编辑器 ───────────────────────────────────────────────
  function renderMarkerList() {
    const host = $('#marker-list');
    const preview = app.preview;
    if (!preview || !preview.markers.editable) {
      host.innerHTML = '<p class="field-hint">切换成可编辑排版后，这里会列出每个分组的起始方块。</p>';
      return;
    }
    host.innerHTML = preview.markers.items.map((marker) => (
      '<div class="marker-row" data-gid="' + marker.gid + '">' +
      '<span class="marker-dot"></span>' +
      '<div class="marker-name"><strong>' + marker.label + '</strong>' +
      '<span>' + escapeHtml((marker.names || []).join(' / ') || (marker.tracks.length + ' 条轨道')) +
      '</span></div>' +
      '<div class="marker-field"><label>Y</label>' +
      '<input class="marker-input" data-field="y" type="number" step="1" value="' + marker.y + '" /></div>' +
      '<div class="marker-field"><label>Z</label>' +
      '<input class="marker-input" data-field="side" type="number" step="1" value="' + marker.side + '" /></div>' +
      '<button class="marker-locate" type="button">定位到这一组</button>' +
      '</div>'
    )).join('');

    $$('#marker-list .marker-row').forEach((row) => {
      const gid = Number(row.dataset.gid);
      row.addEventListener('click', (event) => {
        if (event.target.tagName === 'INPUT') return;
        selectMarker(gid, true);
      });
      $$('input', row).forEach((input) => {
        input.addEventListener('change', () => {
          const marker = app.preview.markers.items.find((item) => item.gid === gid);
          if (!marker) return;
          const y = clampInt($('[data-field="y"]', row).value, -24, 96, marker.y);
          const side = clampInt($('[data-field="side"]', row).value, -128, 128, marker.side);
          applyMarker(gid, { y, side });
        });
      });
    });
    if (app.editorViewer && app.editorViewer.selected != null) {
      selectMarker(app.editorViewer.selected, false);
    }
  }

  function clampInt(value, low, high, fallback) {
    const number = parseInt(value, 10);
    if (Number.isNaN(number)) return fallback;
    return Math.max(low, Math.min(high, number));
  }

  function selectMarker(gid, focus, from2d) {
    if (!app.preview || !app.preview.markers) return;
    if (app.editorViewer) {
      app.editorViewer.setSelected(gid);
      if (focus && !from2d) app.editorViewer.focusMarker(gid);
    }
    if (app.editor2d) {
      app.editor2d.setSelected(gid);
      if (focus && from2d) app.editor2d.focusMarker && app.editor2d.focusMarker(gid);
    }
    $$('#marker-list .marker-row').forEach((row) => {
      row.classList.toggle('is-selected', Number(row.dataset.gid) === gid);
    });
  }

  function applyMarker(gid, position) {
    if (!app.preview) return;
    const marker = app.preview.markers.items.find((item) => item.gid === gid);
    if (!marker) return;
    marker.y = position.y;
    marker.side = position.side;
    marker.z = position.side;
    if (app.editorViewer) app.editorViewer.updateMarker(gid, position);
    if (app.editor2d) app.editor2d.updateMarker(gid, position);
    const row = $('#marker-list .marker-row[data-gid="' + gid + '"]');
    if (row) {
      $('[data-field="y"]', row).value = position.y;
      $('[data-field="side"]', row).value = position.side;
      row.classList.add('is-selected');
    }
    scheduleOffsets();
  }

  function onMarkerMove(gid, position, final) {
    applyMarker(gid, position);
    if (final) {
      clearTimeout(app.offsetTimer);
      app.offsetTimer = 0;
      saveOffsets();
    }
  }

  const scheduleOffsets = debounce(() => {
    if (app.editorViewer && app.editorViewer.mode === 'marker') return;
    saveOffsets();
  }, 400);

  async function saveOffsets() {
    if (!app.preview || !app.preview.markers) return;
    const offsets = {};
    app.preview.markers.items.forEach((marker) => {
      offsets[marker.gid] = { y: marker.y, side: marker.side };
    });
    try {
      await api('/api/offsets', { body: { offsets: offsets } });
      app.dirty = true;
      if (app.step === 6) {
        // 重新计算结构，让方块跟随新的起点（静默刷新，避免遮罩打断拖拽）
        await ensurePreview(true, true);
      }
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function resetOffsets() {
    try {
      const data = await api('/api/offsets/reset', { body: {} });
      app.dirty = true;
      applyState(data.state);
      if (app.editorViewer) app.editorViewer.selected = null;
      if (app.editor2d) app.editor2d.selected = null;
      await ensurePreview(true);
      toast('轨道位置已恢复默认', 'ok');
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  // ── 导出 ─────────────────────────────────────────────────
  function renderExportSummary() {
    if (!app.state || !app.state.song) return;
    const state = app.state;
    const config = state.config;
    const style = state.styles.find((item) => item.id === config.layout_style);
    const masterCount = (config.curve_master_layers || []).length;
    const items = [
      ['歌曲', state.song.name, state.song.notes + ' 音符 · ' + state.song.seconds + ' 秒'],
      ['音轨', state.tracks.length + ' 条 / ' + state.groups.length + ' 组',
        state.tracks.filter((track) => track.stereo).length + ' 条立体声'],
      ['排版', style ? style.name : config.layout_style,
        config.layout_style === 'curve' ? masterCount + ' 条主音轨' : '传统排版'],
      ['空 tick 方块', prettyBlock(config.empty_block), config.empty_block],
      ['支撑方块', prettyBlock(config.support_block), config.support_block],
      ['粒子特效', config.particles_enabled && (config.particles_layers || []).length
        ? (config.particles_layers || []).length + ' 条音轨'
        : '未启用',
        config.particles_enabled ? '拖尾 ' + config.particles_trail + ' · 高 ' +
          config.particles_height + ' · ' + (config.particles_curve || 'arc') : 'MiauParticleEffects'],
      ['矿车', config.layout_style === 'curve' && config.curve_minecart ? '跟随开启' : '不生成',
        config.layout_style === 'curve' ? '隐形矿车链' : '传统排版无需矿车'],
    ];
    $('#export-summary').innerHTML = items.map((item) => (
      '<div class="summary-item"><span>' + item[0] + '</span><strong>' +
      escapeHtml(item[1]) + '</strong><em>' + escapeHtml(item[2]) + '</em></div>'
    )).join('');
  }

  async function generate() {
    const path = $('#output-path').value.trim();
    if (!path) {
      toast('请填写输出路径', 'error');
      return;
    }
    let ticks = null;
    const rawTicks = $('#export-ticks').value.trim();
    if (rawTicks) {
      const parsed = parseInt(rawTicks, 10);
      if (Number.isNaN(parsed) || parsed < 1) {
        toast('导出长度必须是大于 0 的整数', 'error');
        return;
      }
      ticks = parsed;
    }
    const button = $('#btn-generate');
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = '生成中…';
    try {
      const data = await api('/api/generate', { body: { path: path, ticks: ticks } });
      app.generated = data.result;
      $('#export-result').hidden = false;
      $('#result-path').textContent = data.result.path;
      $('#result-stats').innerHTML = [
        ['方块', data.result.blocks.toLocaleString('zh-CN')],
        ['tick', String(data.result.ticks)],
        ['大小', formatSize(data.result.size)],
      ].map((pair) => '<div><dt>' + pair[0] + '</dt><dd>' + pair[1] + '</dd></div>').join('');
      $('#export-warnings').innerHTML = (data.result.warnings || []).map((text) => (
        '<div class="warning-item">' + escapeHtml(text) + '</div>'
      )).join('');
      app.dirty = true;
      toast('结构已生成', 'ok');
      const dataState = await api('/api/state');
      applyState(dataState.state);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function formatSize(bytes) {
    if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(2) + ' MB';
    if (bytes > 1024) return Math.round(bytes / 1024) + ' KB';
    return bytes + ' B';
  }

  // ── 歌曲 ─────────────────────────────────────────────────
  async function loadSong(path) {
    if (!path) {
      toast('请填写 .nbs 文件路径', 'error');
      return;
    }
    const button = $('#btn-load-song');
    button.disabled = true;
    try {
      const data = await api('/api/song', { body: { path: path } });
      app.preview = null;
      app.dirty = true;
      app.generated = null;
      updateWarningChip([]);
      $('#export-result').hidden = true;
      applyState(data.state);
      toast('已载入 ' + data.state.song.name, 'ok');
      go(2);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function pickFile(kind, targetInput, autoLoad) {
    try {
      const data = await api('/api/pick', { body: { kind: kind } });
      if (!data.path) return;
      targetInput.value = data.path;
      if (autoLoad) loadSong(data.path);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  // ── 绑定 ─────────────────────────────────────────────────
  function bindStatic() {
    bindPreviewControls();
    bindBlockPickers();
    bindParticleControls();
    $('#btn-load-song').addEventListener('click', () => loadSong($('#song-path').value.trim()));
    $('#song-path').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') loadSong($('#song-path').value.trim());
    });
    $('#btn-pick-song').addEventListener('click', () =>
      pickFile('nbs', $('#song-path'), true));
    $('#btn-pick-output').addEventListener('click', () =>
      pickFile('schem', $('#output-path'), false));
    $('#btn-generate').addEventListener('click', generate);
    $('#btn-open-folder').addEventListener('click', async () => {
      if (!app.generated) return;
      try {
        await api('/api/open-folder', { body: { path: app.generated.path } });
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    $('#btn-again').addEventListener('click', () => {
      go(1);
      $('#song-path').focus();
    });
    $('#brand-home').addEventListener('click', (event) => {
      event.preventDefault();
      go(1);
    });
    $('#btn-reload').addEventListener('click', () => location.reload());
    $('#chip-warnings').addEventListener('click', () => {
      if (app.step !== 5) go(5);
      setTimeout(() => {
        const target = $('#preview-warnings');
        if (target && target.firstElementChild) {
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }, 140);
    });
    $('#editor-reset-offsets').addEventListener('click', resetOffsets);
    $('#track-stereo').addEventListener('change', onStereoChange);
    $('#track-master').addEventListener('change', onMasterChange);
    $('#track-split').addEventListener('click', moveSelectedToOwnGroup);

    $$('#auto-group .seg').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          const data = await api('/api/auto-group', {
            body: { mode: Number(button.dataset.mode) },
          });
          app.dirty = true;
          applyState(data.state);
          toast('已重新分组', 'ok');
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    });

    const dialog = $('#help-dialog');
    $('#btn-help').addEventListener('click', () => dialog.showModal());
    $('#help-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });

    window.addEventListener('resize', debounce(() => {
      if (app.previewViewer) app.previewViewer.resize();
      if (app.editorViewer) app.editorViewer.resize();
      if (app.editor2d) app.editor2d.resize();
    }, 150));
  }

  async function boot() {
    renderRail();
    bindStatic();
    try {
      const data = await api('/api/state');
      applyState(data.state);
      go(1);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  boot();
})();
