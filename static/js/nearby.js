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

    function createNearby(boot) {
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
            activePoi: '',

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
        let scriptPromise = null;
        let infoWindow = null;
        let autoLocateTried = false;

        const hasItems = computed(() => nb.items.length > 0);
        const distLabel = computed(() => {
            const r = Number(nb.radius) || 0;
            return r >= 1000 ? `${(r / 1000).toFixed(r % 1000 ? 1 : 0)}km` : `${r}m`;
        });

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
                nb.mapReady = true;
                nb.mapError = '';
                return map;
            } catch (err) {
                nb.mapError = err.message || '地图不可用';
                return null;
            }
        }

        async function renderMarkers() {
            const instance = await ensureMap();
            if (!instance || !window.AMap) return;
            const AMap = window.AMap;
            amapMarkers.forEach((m) => m.setMap(null));
            amapMarkers = [];

            if (nb.lng && nb.lat) {
                const me = new AMap.Marker({
                    position: [nb.lng, nb.lat],
                    icon: 'https://webapi.amap.com/theme/v1.3/markers/n/mark_b.png',
                    title: '我的位置',
                    zIndex: 200,
                });
                me.setMap(instance);
                amapMarkers.push(me);
            }

            nb.items.slice(0, 40).forEach((place, idx) => {
                const marker = new AMap.Marker({
                    position: [place.lng, place.lat],
                    title: place.name,
                    label: { content: `${idx + 1}`, direction: 'top' },
                    zIndex: 100 - idx,
                });
                marker.on('click', () => {
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
                    nb.geoStatus = `定位成功（精度 ±${Math.round(pos.coords.accuracy)}m）`;
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
                    nb.geoStatus = `IP 定位：${data.city || data.province || '未知'}（城市级精度）`;
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
                nb.geoStatus = '已使用默认位置（雅安市区），可手动输入地址';
                await reverseGeocode();
                await search();
            }
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
            search();
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
                nb.address = [tip.district, tip.address].filter(Boolean).join(' ');
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

        function closeDetail() {
            nb.detail = null;
            nb.activePoi = '';
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
                nb.message = data.is_favorite ? '已收藏' : '已取消收藏';
            } catch (err) {
                nb.message = `操作失败：${err.message}`;
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
                nb.message = '打卡评价已发布';
            } catch (err) {
                nb.message = `打卡失败：${err.message}`;
            } finally {
                nb.submitting = false;
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
            const instance = await ensureMap();
            if (!instance) return;
            instance.setZoomAndCenter(16, [place.lng, place.lat]);
        }

        async function openMap() {
            await ensureMap();
            if (map) map.resize();
        }

        /** 首次进入自动定位一次：GPS → IP → 默认位置，避免用户面对空白列表 */
        async function autoLocate() {
            if (autoLocateTried || nb.located) return;
            autoLocateTried = true;
            await locateByGPS();
        }

        return {
            nb,
            hasItems,
            distLabel,
            init,
            autoLocate,
            locateByGPS,
            locateByIP,
            search,
            setRadius,
            onKeywordInput,
            pickTip,
            openPlace,
            closeDetail,
            toggleFavorite,
            submitReview,
            navigate,
            focusOnMap,
            openMap,
        };
    }

    window.FoodNearby = { createNearby };
})();
