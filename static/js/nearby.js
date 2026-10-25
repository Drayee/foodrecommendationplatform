/* ===========================================================
   寻味 · 附近美食（高德 POI）
   独立模块：只依赖 Vue + axios，逻辑集中在 createNearby()，
   主应用通过 window.FoodNearby.createNearby(API, boot) 接入。
   =========================================================== */
(function () {
    'use strict';

    const { reactive, computed, nextTick } = Vue;
    const BASE = '/api/v1/places';

    function unwrap(promise) {
        return promise.then(
            (resp) => {
                const body = resp.data || {};
                if (body.code === 0) return body.data;
                const err = new Error(body.msg || '请求失败');
                err.code = body.code;
                throw err;
            },
            (e) => {
                const body = (e.response && e.response.data) || {};
                const err = new Error(body.msg || `请求失败（${e.response ? e.response.status : '网络'}）`);
                err.code = body.code;
                throw err;
            }
        );
    }

    const http = axios.create({ baseURL: BASE, timeout: 20000, withCredentials: true });

    function debounce(fn, wait) {
        let timer = null;
        return function debounced(...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), wait);
        };
    }

    function createNearby(boot, opts) {
        const options = opts || {};
        const nb = reactive({
            inited: false,
            loading: false,
            located: false,
            lng: 0,
            lat: 0,
            address: '',
            addressDetail: '',
            geoStatus: '尚未定位',
            geoError: '',

            radius: 3000,
            keyword: '',
            type: 'all',
            sort: 'smart',
            budget: '',

            items: [],
            total: 0,
            degraded: false,
            provider: '',
            latency: 0,
            message: '',
            keyPool: { available: 0, total: 0 },
            fromCache: false,

            tips: [],
            showTips: false,
            placeFavorites: [],
            myReviews: [],

            detail: null,
            detailLoading: false,
            isFavorite: false,
            reviews: [],
            reviewStats: { count: 0, avg_rating: 0, favorited: 0 },
            myRating: 5,
            myContent: '',
            myCost: '',
            myDishes: '',
            submitting: false,

            mapReady: false,
            mapError: '',
            mapMode: 'map',          // 'map' | 'list' —— list = 折叠地图，纯列表模式
            mapHint: '手指/鼠标点地图任意位置，即以此为圆心重新搜索',
            activePoi: '',
            centerPulse: 0,          // 每次改中心自增，用于模板 key 触发动画
            pulseVisible: false,
            centerSource: '',        // map | marker | gps | ip | preset | tip
            centerAt: '',            // 最近一次改中心的时间（HH:mm）

            lightbox: '',
            lightboxPhotos: [],
            lightboxIndex: 0,

            sheetUp: !(typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 760px)').matches),
            sheetDrag: null,         // 拖拽中的位移（px），null = 用 CSS class 控制
            sheetDragging: false,

            flash: '',               // 轻量即时反馈文案
            lastSearchAt: 0,

            config: {
                js_key: '',
                security_code: '',
                default_radius: 3000,
                max_radius: 50000,
                radius_options: [500, 1000, 2000, 3000, 5000, 10000],
                types: [{ key: 'all', label: '全部美食', code: '050000' }],
                sorts: [
                    { key: 'smart', label: '综合推荐' },
                    { key: 'distance', label: '距离最近' },
                ],
            },
        });

        let map = null;
        let amapMarkers = [];
        let centerMarker = null;
        let pulseCircle = null;
        let pulseAnimTimer = null;
        let pulseHideTimer = null;
        let flashTimer = null;
        let lightboxKeyBound = false;
        let clickGuard = 0;
        let skipFitOnce = false;
        let scriptPromise = null;
        let infoWindow = null;
        let autoLocateTried = false;

        const hasItems = computed(() => nb.items.length > 0);
        const distLabel = computed(() => {
            const r = Number(nb.radius) || 0;
            return r >= 1000 ? `${(r / 1000).toFixed(r % 1000 ? 1 : 0)}km` : `${r}m`;
        });
        const searchingLabel = computed(() => `正在搜索附近 ${distLabel.value}…`);
        /** 首次进入 / 定位失败且无数据 → 显示定位引导卡 */
        const showGuide = computed(() => !nb.located && !nb.loading);
        /** 已定位但当前范围内没有结果 */
        const showEmptyResult = computed(() => nb.located && !nb.items.length && !nb.loading);
        const degradedText = computed(
            () => nb.message || '高德服务暂时不可用，当前展示离线样例数据，不代表真实店铺'
        );
        const sheetPeek = 128;

        /* ---------------- 高德 JS API（多 Key 轮换） ---------------- */
        const JS_PAIR_STORAGE = 'xunwei.amap.jsPair';

        function jsPairs() {
            const pairs = (nb.config.js_pairs || []).filter((p) => p && p.key);
            if (pairs.length) return pairs;
            // 兼容只有单 Key + 单一安全密钥的旧配置
            if (nb.config.js_key) {
                return [{ key: nb.config.js_key, security_code: nb.config.security_code || '' }];
            }
            return [];
        }

        function injectAmapScript(pair) {
            return new Promise((resolve, reject) => {
                window._AMapSecurityConfig = { securityJsCode: pair.security_code || '' };
                const script = document.createElement('script');
                script.dataset.amap = '1';
                script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(pair.key)}&plugin=AMap.Scale,AMap.ToolBar`;
                script.async = true;
                script.onload = () => (window.AMap ? resolve(window.AMap) : reject(new Error('高德地图对象缺失')));
                script.onerror = () => reject(new Error('高德地图脚本加载失败（Key 配额/域名白名单/网络）'));
                document.head.appendChild(script);
            }).catch((err) => {
                // 失败时摘掉脚本，避免脏节点影响下一个 Key 重试
                document.querySelectorAll('script[data-amap="1"]').forEach((el) => el.remove());
                throw err;
            });
        }

        async function loadAMap() {
            if (window.AMap) return window.AMap;
            if (scriptPromise) return scriptPromise;

            const pairs = jsPairs();
            if (!pairs.length) throw new Error('未配置高德 JS API Key');

            const start = Number(sessionStorage.getItem(JS_PAIR_STORAGE) || 0) % pairs.length;
            scriptPromise = (async () => {
                let lastError = new Error('高德地图加载失败');
                for (let i = 0; i < pairs.length; i += 1) {
                    const idx = (start + i) % pairs.length;
                    try {
                        const amap = await injectAmapScript(pairs[idx]);
                        sessionStorage.setItem(JS_PAIR_STORAGE, String(idx));
                        nb.jsPairIndex = idx;
                        return amap;
                    } catch (err) {
                        lastError = err;
                        console.warn(`[amap] JS Key #${idx + 1} 加载失败，尝试下一个：`, err.message);
                    }
                }
                throw lastError;
            })().catch((err) => {
                scriptPromise = null;
                throw err;
            });
            return scriptPromise;
        }

        async function ensureMap() {
            if (!nb.inited) await init();
            if (nb.mapMode === 'list') return map;      // 纯列表模式不初始化地图，省流量
            if (map) return map;
            try {
                const AMap = await loadAMap();
                const center = nb.lng && nb.lat ? [nb.lng, nb.lat] : [103.0012, 29.9875];
                map = new AMap.Map('amap-container', {
                    zoom: 14,
                    center,
                    viewMode: '2D',
                    resizeEnable: true,
                });
                map.addControl(new AMap.Scale());
                map.addControl(new AMap.ToolBar({ position: 'RB' }));
                infoWindow = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -28) });

                /* ---- 核心：点击地图任意点 → 以该点为圆心重新搜索 ---- */
                map.on('click', (e) => {
                    if (Date.now() - clickGuard < 260) return;   // 标记/拖拽的余波不算点选
                    const lnglat = e && e.lnglat;
                    if (!lnglat) return;
                    recenter(lnglat.getLng(), lnglat.getLat(), 'map');
                });

                nb.mapReady = true;
                nb.mapError = '';
                syncCenterVisual(false);
                return map;
            } catch (err) {
                nb.mapError = err.message || '地图不可用';
                return null;
            }
        }

        /* ---------------- 中心点视觉反馈 ---------------- */
        function ensureCenterMarker() {
            if (!map || !window.AMap || nb.mapMode === 'list') return;
            const AMap = window.AMap;
            if (!nb.lng || !nb.lat) return;
            if (!centerMarker) {
                centerMarker = new AMap.Marker({
                    position: [nb.lng, nb.lat],
                    content: '<div class="amap-center-pin"><i></i><em>搜索中心</em></div>',
                    offset: new AMap.Pixel(-14, -34),
                    draggable: true,
                    cursor: 'move',
                    zIndex: 260,
                    bubble: true,
                });
                // 拖动中心标记 → 松手即以新位置重新搜索
                centerMarker.on('dragend', (e) => {
                    clickGuard = Date.now();
                    const lnglat = e && e.lnglat;
                    if (!lnglat) return;
                    recenter(lnglat.getLng(), lnglat.getLat(), 'marker');
                });
                centerMarker.on('click', () => { clickGuard = Date.now(); });
            }
            centerMarker.setPosition([nb.lng, nb.lat]);
            centerMarker.setMap(map);
        }

        /** 地图上的橙色波纹：让用户明确知道「点生效了」 */
        function pulseOnMap(lng, lat) {
            if (!map || !window.AMap || nb.mapMode === 'list') return;
            const AMap = window.AMap;
            if (pulseCircle) {
                try { pulseCircle.setMap(null); } catch (e) { /* ignore */ }
                pulseCircle = null;
            }
            clearInterval(pulseAnimTimer);
            pulseCircle = new AMap.Circle({
                center: [lng, lat],
                radius: 24,
                strokeColor: '#fa541c',
                strokeWeight: 2,
                strokeOpacity: 0.95,
                fillColor: '#fa541c',
                fillOpacity: 0.26,
                zIndex: 300,
                bubble: true,
            });
            pulseCircle.setMap(map);
            let radius = 24;
            let step = 0;
            pulseAnimTimer = setInterval(() => {
                step += 1;
                radius += 24;
                const fade = Math.max(0, 1 - step / 15);
                try {
                    pulseCircle.setRadius(radius);
                    pulseCircle.setOptions({ strokeOpacity: fade, fillOpacity: fade * 0.26 });
                } catch (e) { /* ignore */ }
                if (step >= 15) {
                    clearInterval(pulseAnimTimer);
                    try { pulseCircle.setMap(null); } catch (e) { /* ignore */ }
                    pulseCircle = null;
                }
            }, 55);
        }

        function syncCenterVisual(withPulse) {
            ensureCenterMarker();
            if (withPulse) {
                nb.pulseVisible = true;
                nb.centerPulse = Date.now();
                clearTimeout(pulseHideTimer);
                pulseHideTimer = setTimeout(() => { nb.pulseVisible = false; }, 1800);
                pulseOnMap(nb.lng, nb.lat);
            }
        }

        /** 统一入口：改中心 + 反查地址 + 重新搜索（地图点选 / 标记拖拽 都走这里） */
        async function recenter(lng, lat, source) {
            applyCenter(lng, lat, false);
            nb.centerSource = source || 'map';
            nb.centerAt = new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
            if (map) {
                try { map.setCenter([nb.lng, nb.lat]); } catch (e) { /* ignore */ }
            }
            skipFitOnce = true;          // 手动指定的中心不要被 setFitView 抢走
            syncCenterVisual(true);
            flash(
                source === 'marker' ? '中心点已拖动，正在重新搜索…' : '已切换到该位置，正在重新搜索…',
                1800
            );
            await reverseGeocode();
            await search();
        }

        async function renderMarkers() {
            const instance = await ensureMap();
            if (!instance || !window.AMap) return;
            const AMap = window.AMap;
            amapMarkers.forEach((m) => m.setMap(null));
            amapMarkers = [];

            // 中心点用可拖拽的专用标记（搜索圆心），不再重复画"我的位置"
            ensureCenterMarker();

            nb.items.slice(0, 40).forEach((place, idx) => {
                const marker = new AMap.Marker({
                    position: [place.lng, place.lat],
                    title: place.name,
                    label: { content: `${idx + 1}`, direction: 'top' },
                    zIndex: 100 - idx,
                });
                marker.on('click', () => {
                    clickGuard = Date.now();
                    nb.activePoi = place.poi_id;
                    infoWindow.setContent(
                        `<div style="padding:6px 8px;font-size:13px;line-height:1.6">
                            <b>${place.name}</b><br>
                            <span style="color:#888">${place.type_label || ''} · ${place.distance_text || ''}</span>
                            ${place.rating ? `<br>★ ${place.rating}　人均 ¥${place.cost || '-'}` : ''}
                         </div>`
                    );
                    infoWindow.open(instance, [place.lng, place.lat]);
                    openPlace(place);
                });
                marker.setMap(instance);
                amapMarkers.push(marker);
            });

            if (skipFitOnce) {
                // 用户刚点选/拖动过中心：保持该中心，不自动缩放视野
                skipFitOnce = false;
                try {
                    instance.setCenter([nb.lng, nb.lat]);
                } catch (err) { /* ignore */ }
                return;
            }

            if (nb.items.length) {
                // setFitView 只接受覆盖物数组或经纬度边界，传错会抛错 —— 单独兜住，绝不影响列表
                try {
                    instance.setFitView(amapMarkers, false, [60, 60, 60, 60]);
                } catch (err) {
                    console.warn('[amap] setFitView 失败，忽略：', err && err.message);
                    if (nb.lng && nb.lat) instance.setCenter([nb.lng, nb.lat]);
                }
            } else if (nb.lng && nb.lat) {
                instance.setCenter([nb.lng, nb.lat]);
            }
        }

        /* ---------------- 配置 ---------------- */
        async function init() {
            if (nb.inited) return;
            nb.inited = true;
            try {
                const cfg = await unwrap(http.get('/config/'));
                nb.config = { ...nb.config, ...cfg };
                nb.keyPool = { available: cfg.key_pool ? cfg.key_pool.web_available : 0, total: cfg.key_pool ? cfg.key_pool.web_total : 0 };
                nb.radius = cfg.default_radius || 3000;
                if (cfg.enabled === false) nb.message = '未配置高德 Web 服务 Key，当前为离线样例模式';
            } catch (err) {
                nb.message = `配置加载失败：${err.message}`;
            }
        }

        /* ---------------- 定位 ---------------- */
        function applyCenter(lng, lat, fromWgs84) {
            nb.lng = Number(lng);
            nb.lat = Number(lat);
            nb.located = true;
            nb.fromWgs84 = !!fromWgs84;
        }

        /** 轻量反馈：页面内 flash + 主应用 toast（有就弹） */
        function flash(text, ms) {
            nb.flash = text || '';
            clearTimeout(flashTimer);
            flashTimer = setTimeout(() => { nb.flash = ''; }, ms || 2400);
            if (typeof options.toast === 'function' && text) {
                try { options.toast(text, ms || 2400); } catch (e) { /* ignore */ }
            }
        }

        async function reverseGeocode() {
            if (!nb.lng || !nb.lat) return;
            try {
                const data = await unwrap(http.get('/regeo/', { params: { lng: nb.lng, lat: nb.lat, coord: 'gcj02' } }));
                const addr = (data && data.address) || {};
                nb.address = addr.formatted || [addr.province, addr.city, addr.district].filter(Boolean).join('');
                nb.addressDetail = addr.nearest || (addr.business_areas || [])[0] || '';
            } catch (err) {
                nb.address = nb.address || '位置已获取';
            }
        }

        function locateByGPS() {
            if (!navigator.geolocation) {
                nb.geoError = '当前浏览器不支持定位';
                return locateByIP();
            }
            nb.geoStatus = '正在获取定位…';
            nb.geoError = '';
            navigator.geolocation.getCurrentPosition(
                async (pos) => {
                    applyCenter(pos.coords.longitude, pos.coords.latitude, true);
                    nb.centerSource = 'gps';
                    nb.geoStatus = `定位成功（精度 ±${Math.round(pos.coords.accuracy)}m）`;
                    syncCenterVisual(true);
                    await reverseGeocode();
                    await search();
                },
                (err) => {
                    const map = { 1: '你拒绝了定位授权', 2: '定位不可用（信号弱）', 3: '定位超时' };
                    nb.geoError = map[err.code] || err.message || '定位失败';
                    nb.geoStatus = 'GPS 失败，已尝试 IP 定位';
                    locateByIP();
                },
                { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
            );
        }

        async function locateByIP() {
            try {
                const data = await unwrap(http.get('/locate/'));
                const center = (data && data.center) || {};
                if (center.lng && center.lat) {
                    applyCenter(center.lng, center.lat, false);
                    nb.centerSource = 'ip';
                    nb.geoStatus = `IP 定位：${data.city || data.province || '未知'}（城市级精度）`;
                    syncCenterVisual(true);
                    await reverseGeocode();
                    await search();
                    return;
                }
                nb.geoStatus = 'IP 定位未返回坐标';
            } catch (err) {
                nb.geoStatus = `定位失败：${err.message}`;
            }
            if (!nb.located) {
                // 最后兜底：默认雅安市区，保证页面可用
                applyCenter(103.0012, 29.9875, false);
                nb.centerSource = 'preset';
                nb.geoStatus = '已使用默认位置（雅安市区），可点地图换位置';
                syncCenterVisual(false);
                await reverseGeocode();
                await search();
            }
        }

        /** 定位失败时的兜底：直接用默认城市中心，别让用户对着空白页 */
        async function useDefaultCenter() {
            applyCenter(103.0012, 29.9875, false);
            nb.centerSource = 'preset';
            nb.geoStatus = '已使用默认位置（雅安市区），可点地图换位置';
            nb.geoError = '';
            syncCenterVisual(true);
            await reverseGeocode();
            await search();
        }

        /* ---------------- 搜索 ---------------- */
        async function search() {
            if (!nb.lng || !nb.lat) {
                nb.message = '请先定位或输入地址';
                return;
            }
            nb.loading = true;
            nb.showTips = false;
            try {
                const params = {
                    lng: nb.lng,
                    lat: nb.lat,
                    coord: 'gcj02',
                    radius: nb.radius,
                    sort: nb.sort,
                    type: nb.type,
                };
                if (nb.keyword) params.q = nb.keyword;
                if (nb.budget) params.budget = nb.budget;
                const data = await unwrap(http.get('/nearby/', { params }));
                nb.items = data.items || [];
                nb.total = data.total || nb.items.length;
                nb.degraded = !!data.degraded;
                nb.provider = data.provider || '';
                nb.latency = data.latency_ms || 0;
                nb.message = data.message || '';
                nb.fromCache = !!data.cached;
                if (data.key_pool) nb.keyPool = data.key_pool;
            } catch (err) {
                // 保留上一次的列表，避免一次抖动把页面清空
                nb.message = `搜索失败：${err.message}`;
            } finally {
                nb.loading = false;
                nb.lastSearchAt = Date.now();
            }
            // 地图渲染与列表彻底解耦：地图挂了不影响数据展示
            try {
                await renderMarkers();
            } catch (err) {
                nb.mapError = nb.mapError || err.message || '地图标记渲染失败';
            }
        }

        function setRadius(value) {
            nb.radius = Number(value);
            return preserveScroll(() => search());
        }

        /** 排序/筛选切换：保留列表滚动位置，别把用户甩回顶部 */
        async function preserveScroll(task) {
            const listEl = document.querySelector('.nearby-list');
            const winY = window.scrollY || window.pageYOffset || 0;
            const ownY = listEl ? listEl.scrollTop : 0;
            const wrapEl = listEl ? listEl.parentElement : null;
            const wrapY = wrapEl ? wrapEl.scrollTop : 0;
            await task();
            await nextTick();
            if (listEl) listEl.scrollTop = ownY;
            if (wrapEl) wrapEl.scrollTop = wrapY;
            window.scrollTo({ top: winY, left: 0, behavior: 'auto' });
        }

        /** 模板里 select 的 @change 统一走这里（v-model 已先更新值） */
        function applyFilter() {
            return preserveScroll(() => search());
        }

        /** 离线样例重试 */
        function retrySearch() {
            flash('正在重试…', 1600);
            return preserveScroll(() => search());
        }

        const fetchTips = debounce(async () => {
            const q = (nb.keyword || '').trim();
            if (q.length < 2) {
                nb.tips = [];
                nb.showTips = false;
                return;
            }
            try {
                const data = await unwrap(http.get('/tips/', { params: { q, lng: nb.lng, lat: nb.lat } }));
                nb.tips = (data.items || []).slice(0, 8);
                nb.showTips = nb.tips.length > 0;
            } catch (err) {
                nb.tips = [];
            }
        }, 320);

        function onKeywordInput() {
            fetchTips();
        }

        async function pickTip(tip) {
            nb.showTips = false;
            nb.keyword = tip.name;
            if (tip.location && tip.location.includes(',')) {
                const [lng, lat] = tip.location.split(',').map(Number);
                applyCenter(lng, lat, false);
                nb.centerSource = 'tip';
                nb.address = [tip.district, tip.address].filter(Boolean).join(' ');
                if (map) {
                    try { map.setCenter([lng, lat]); } catch (e) { /* ignore */ }
                }
                skipFitOnce = true;
                syncCenterVisual(true);
                await search();
            } else {
                await search();
            }
        }

        /* ---------------- 详情 / 收藏 / 打卡 ---------------- */
        async function openPlace(place) {
            nb.activePoi = place.poi_id;
            nb.detail = place;
            nb.detailLoading = true;
            nb.myRating = 5;
            nb.myContent = '';
            nb.myCost = place.cost ? String(place.cost) : '';
            nb.myDishes = '';
            if (!nb.sheetUp && isMobile()) nb.sheetUp = true;   // 手机上点结果 → 抽屉自动展开
            scrollCardIntoView(place.poi_id);
            try {
                const data = await unwrap(http.get(`/${place.poi_id}/`));
                if (data.place) nb.detail = { ...place, ...data.place };
                nb.reviews = data.reviews || [];
                nb.reviewStats = data.review_stats || { count: 0, avg_rating: 0, favorited: 0 };
                nb.isFavorite = !!data.is_favorite;
                http.post('/track/', { action: 'view', poi_id: place.poi_id, name: place.name }).catch(() => {});
            } catch (err) {
                nb.reviews = [];
            } finally {
                nb.detailLoading = false;
            }
        }

        /** 高亮卡片滚动到可见区域（点地图标记时列表可能在折叠抽屉里） */
        function scrollCardIntoView(poiId) {
            if (!poiId) return;
            nextTick(() => {
                const el = document.querySelector(`.place-card[data-poi="${poiId}"]`);
                if (el && el.scrollIntoView) {
                    try {
                        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                    } catch (e) {
                        el.scrollIntoView(false);
                    }
                }
            });
        }

        function closeDetail() {
            closePhoto();
            nb.detail = null;
            nb.activePoi = '';
        }

        /** 评分分布条宽度（百分比） */
        function distPercent(star) {
            const dist = (nb.reviewStats && nb.reviewStats.distribution) || {};
            const total = Object.values(dist).reduce((a, b) => a + (Number(b) || 0), 0);
            if (!total) return 0;
            return Math.round(((Number(dist[String(star)]) || 0) / total) * 100);
        }

        /* ---------------- 照片预览：Esc 关闭 + 左右切换 ---------------- */
        function lightboxPhotos() {
            const photos = (nb.detail && nb.detail.photos) || [];
            if (photos.length) return photos.slice();
            return nb.lightbox ? [nb.lightbox] : [];
        }

        function onLightboxKey(e) {
            if (!nb.lightbox) return;
            if (e.key === 'Escape') {
                e.preventDefault();
                closePhoto();
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                stepPhoto(1);
            } else if (e.key === 'ArrowLeft') {
                e.preventDefault();
                stepPhoto(-1);
            }
        }

        function bindLightboxKeys() {
            if (lightboxKeyBound) return;
            document.addEventListener('keydown', onLightboxKey);
            lightboxKeyBound = true;
        }

        function unbindLightboxKeys() {
            if (!lightboxKeyBound) return;
            document.removeEventListener('keydown', onLightboxKey);
            lightboxKeyBound = false;
        }

        function openPhoto(url, index) {
            const photos = lightboxPhotos();
            nb.lightboxPhotos = photos;
            let idx = typeof index === 'number' ? index : photos.indexOf(url);
            if (!(idx >= 0)) idx = 0;
            nb.lightboxIndex = idx;
            nb.lightbox = photos[idx] || url || '';
            bindLightboxKeys();
        }

        function stepPhoto(delta) {
            const photos = nb.lightboxPhotos && nb.lightboxPhotos.length ? nb.lightboxPhotos : lightboxPhotos();
            if (photos.length < 2) return;
            nb.lightboxPhotos = photos;
            nb.lightboxIndex = (nb.lightboxIndex + delta + photos.length) % photos.length;
            nb.lightbox = photos[nb.lightboxIndex];
        }

        function nextPhoto() { stepPhoto(1); }
        function prevPhoto() { stepPhoto(-1); }

        function closePhoto() {
            nb.lightbox = '';
            unbindLightboxKeys();
        }

        async function toggleFavorite() {
            if (!nb.detail) return;
            if (!boot.isAuthenticated) {
                window.location.href = '/';
                return;
            }
            try {
                const data = await unwrap(
                    http.post(`/${nb.detail.poi_id}/favorite/`, { place: nb.detail })
                );
                nb.isFavorite = !!data.is_favorite;
                nb.reviewStats.favorited = data.count || 0;
                flash(nb.isFavorite ? '❤️ 已收藏，可在「我的」查看' : '🤍 已取消收藏', 2000);
            } catch (err) {
                flash(`操作失败：${err.message}`, 2600);
            }
        }

        async function submitReview() {
            if (!nb.detail) return;
            if (!boot.isAuthenticated) {
                window.location.href = '/';
                return;
            }
            nb.submitting = true;
            try {
                const dishes = (nb.myDishes || '')
                    .split(/[,，、\s]+/)
                    .map((s) => s.trim())
                    .filter(Boolean);
                const review = await unwrap(
                    http.post(`/${nb.detail.poi_id}/reviews/`, {
                        rating: Number(nb.myRating) || 5,
                        content: nb.myContent,
                        place_name: nb.detail.name,
                        cost: Number(nb.myCost) || 0,
                        dishes,
                    })
                );
                nb.reviews.unshift(review);
                nb.reviewStats.count = (nb.reviewStats.count || 0) + 1;
                nb.myContent = '';
                nb.myDishes = '';
                flash('✅ 打卡评价已发布', 2200);
            } catch (err) {
                flash(`打卡失败：${err.message}`, 2600);
            } finally {
                nb.submitting = false;
            }
        }

        /* ---------------- 我收藏的店 ---------------- */
        async function loadPlaceFavorites() {
            if (!boot.isAuthenticated) {
                nb.placeFavorites = [];
                return;
            }
            try {
                nb.placeFavorites = (await unwrap(http.get('/favorites/'))) || [];
            } catch (err) {
                nb.placeFavorites = [];
            }
        }

        async function removeFavorite(poiId) {
            try {
                await unwrap(http.post(`/${poiId}/favorite/`, {}));
                nb.placeFavorites = nb.placeFavorites.filter((p) => p.poi_id !== poiId);
                if (nb.detail && nb.detail.poi_id === poiId) nb.isFavorite = false;
                flash('🤍 已取消收藏', 2000);
            } catch (err) {
                flash(`操作失败：${err.message}`, 2600);
            }
        }

        async function loadMyReviews() {
            if (!boot.isAuthenticated) {
                nb.myReviews = [];
                return;
            }
            try {
                nb.myReviews = (await unwrap(http.get('/reviews/mine/'))) || [];
            } catch (err) {
                nb.myReviews = [];
            }
        }

        /* ---------------- 导航 / 叫车 ---------------- */
        function navigate(place, mode) {
            if (!place) return;
            const target = `${place.lng},${place.lat},${encodeURIComponent(place.name || '目的地')}`;
            const url =
                `https://uri.amap.com/navigation?to=${target}` +
                `&mode=${mode || 'car'}&policy=1&src=xunwei&coordinate=gaode&callnative=1`;
            window.open(url, '_blank', 'noopener');
            http.post('/track/', { action: 'route', poi_id: place.poi_id, name: place.name }).catch(() => {});
        }

        /* ---------------- 地图交互 ---------------- */
        async function focusOnMap(place) {
            if (!place) return;
            if (nb.mapMode === 'list') await useMapMode();
            const instance = await ensureMap();
            if (!instance) return;
            instance.setZoomAndCenter(16, [place.lng, place.lat]);
        }

        async function openMap() {
            await ensureMap();
            if (map) map.resize();
        }

        /** 地图挂了 / 用户想专心看列表：折叠地图区域 */
        async function useListMode() {
            nb.mapMode = 'list';
            await nextTick();
            flash('已切换为列表模式，地图已折叠', 2000);
        }

        async function useMapMode() {
            nb.mapMode = 'map';
            await nextTick();
            const instance = await ensureMap();
            if (instance) {
                try { instance.resize(); } catch (e) { /* ignore */ }
                if (nb.lng && nb.lat) instance.setCenter([nb.lng, nb.lat]);
            }
        }

        function toggleMapMode() {
            return nb.mapMode === 'list' ? useMapMode() : useListMode();
        }

        /** 地图脚本失败后重载（换 Key / 恢复网络） */
        async function retryMap() {
            nb.mapError = '';
            scriptPromise = null;
            document.querySelectorAll('script[data-amap="1"]').forEach((el) => el.remove());
            delete window.AMap;
            map = null;
            centerMarker = null;
            if (nb.mapMode === 'list') await useMapMode();
            const instance = await ensureMap();
            if (instance) {
                flash('地图已重新加载', 1800);
                await renderMarkers();
            } else {
                flash('地图仍不可用，可继续使用列表模式', 2400);
            }
        }

        /* ---------------- 手机端底部抽屉 ---------------- */
        function isMobile() {
            return window.matchMedia('(max-width: 760px)').matches;
        }

        function toggleSheet() {
            nb.sheetUp = !nb.sheetUp;
            nb.sheetDrag = null;
        }

        function sheetTouchStart(e) {
            if (!isMobile() || !e.touches || !e.touches.length) return;
            const handleEl = e.currentTarget;
            const wrap = (handleEl && handleEl.closest ? handleEl.closest('.nearby-list-wrap') : null) || handleEl;
            if (!wrap) return;
            const rect = wrap.getBoundingClientRect();
            const base = rect.top - (window.innerHeight - rect.height);
            sheetTouch = {
                startY: e.touches[0].clientY,
                base: Math.max(0, base),
                height: rect.height,
                moved: false,
            };
            nb.sheetDragging = true;
        }

        function sheetTouchMove(e) {
            if (!sheetTouch || !e.touches || !e.touches.length) return;
            const max = Math.max(0, sheetTouch.height - sheetPeek);
            const dy = e.touches[0].clientY - sheetTouch.startY;
            if (Math.abs(dy) > 4) sheetTouch.moved = true;
            nb.sheetDrag = Math.round(Math.min(max, Math.max(0, sheetTouch.base + dy)));
            if (sheetTouch.moved && e.cancelable) e.preventDefault();
        }

        function sheetTouchEnd() {
            if (!sheetTouch) return;
            const max = Math.max(0, sheetTouch.height - sheetPeek);
            const resting = nb.sheetDrag == null ? (nb.sheetUp ? 0 : max) : nb.sheetDrag;
            nb.sheetUp = resting < max * 0.55;
            nb.sheetDrag = null;
            nb.sheetDragging = false;
            sheetTouch = null;
        }

        let sheetTouch = null;

        /** 首次进入自动定位一次：GPS → IP → 默认位置，避免用户面对空白列表 */
        async function autoLocate() {
            if (autoLocateTried || nb.located) return;
            autoLocateTried = true;
            await locateByGPS();
        }

        const api = {
            nb,
            hasItems,
            distLabel,
            searchingLabel,
            showGuide,
            showEmptyResult,
            degradedText,
            init,
            autoLocate,
            locateByGPS,
            locateByIP,
            useDefaultCenter,
            search,
            setRadius,
            applyFilter,
            retrySearch,
            onKeywordInput,
            pickTip,
            recenter,
            openPlace,
            closeDetail,
            toggleFavorite,
            submitReview,
            navigate,
            focusOnMap,
            openMap,
            toggleMapMode,
            useListMode,
            useMapMode,
            retryMap,
            toggleSheet,
            sheetTouchStart,
            sheetTouchMove,
            sheetTouchEnd,
            loadPlaceFavorites,
            loadMyReviews,
            removeFavorite,
            distPercent,
            openPhoto,
            closePhoto,
            nextPhoto,
            prevPhoto,
            flash,
            scrollCardIntoView,
        };

        // 便于自动化验收 / 现场排查：只读暴露地图句柄，不参与业务逻辑
        window.FoodNearby.__last = {
            get map() { return map; },
            get centerMarker() { return centerMarker; },
            get pulseCircle() { return pulseCircle; },
            nb,
        };
        return api;
    }

    window.FoodNearby = { createNearby };
})();
