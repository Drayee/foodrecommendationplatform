/* ===========================================================
   寻味 · 主应用
   hash 路由：#/home #/search #/recipe/<id> #/assistant #/profile
   =========================================================== */
(function () {
    'use strict';

    const API = window.FoodAPI;
    const UI = window.FoodUI;
    const boot = window.__BOOT__ || {};

    const { createApp, ref, reactive, computed, nextTick, onMounted, watch } = Vue;

    /* ---------------- Markdown（轻量，够聊天用） ---------------- */
    function escapeHtml(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function inlineMd(text) {
        return text
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
            .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    }

    function renderMarkdown(md) {
        const lines = escapeHtml(md).split('\n');
        const out = [];
        let ul = [];
        let ol = [];
        let code = null;

        const flushUl = () => { if (ul.length) { out.push('<ul>' + ul.map((x) => `<li>${x}</li>`).join('') + '</ul>'); ul = []; } };
        const flushOl = () => { if (ol.length) { out.push('<ol>' + ol.map((x) => `<li>${x}</li>`).join('') + '</ol>'); ol = []; } };
        const flushAll = () => { flushUl(); flushOl(); };

        for (const raw of lines) {
            const line = raw.replace(/\s+$/, '');
            if (/^\s*```/.test(line)) {
                if (code === null) { flushAll(); code = []; } else { out.push('<pre><code>' + code.join('\n') + '</code></pre>'); code = null; }
                continue;
            }
            if (code !== null) { code.push(raw); continue; }
            if (!line.trim()) { flushAll(); continue; }

            let m = line.match(/^(#{1,4})\s+(.*)$/);
            if (m) { flushAll(); const lv = Math.min(m[1].length, 4); out.push(`<h${lv}>${inlineMd(m[2])}</h${lv}>`); continue; }
            if (/^\s*[-*+]\s+/.test(line)) { flushOl(); ul.push(inlineMd(line.replace(/^\s*[-*+]\s+/, ''))); continue; }
            if (/^\s*\d+[.)]\s+/.test(line)) { flushUl(); ol.push(inlineMd(line.replace(/^\s*\d+[.)]\s+/, ''))); continue; }
            if (/^\s*>\s?/.test(line)) { flushAll(); out.push(`<blockquote>${inlineMd(line.replace(/^\s*>\s?/, ''))}</blockquote>`); continue; }

            flushAll();
            out.push(`<p>${inlineMd(line)}</p>`);
        }
        if (code !== null) out.push('<pre><code>' + code.join('\n') + '</code></pre>');
        flushAll();
        return out.join('');
    }

    /* ---------------- 工具 ---------------- */
    function formatTime(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        const diff = (Date.now() - d.getTime()) / 1000;
        if (diff < 60) return '刚刚';
        if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
        if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    /* ---------- 附近：店铺封面 / emoji ---------- */
    function placeCover(place) {
        if (place && place.photos && place.photos.length) {
            return {
                backgroundImage: `url("${place.photos[0]}")`,
                backgroundSize: 'cover',
                backgroundPosition: 'center',
            };
        }
        return UI.coverStyle({ title: place.name, id: place.poi_id });
    }

    function placeEmoji(place) {
        return UI.dishEmoji(`${place.name} ${place.type_label || ''} ${place.tag || ''}`);
    }

    function uid() {
        return 'web-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
    }

    const app = createApp({
        delimiters: ['[[', ']]'],
        components: { 'recipe-card': UI.RecipeCard, 'star-rating': UI.StarRating },

        setup() {
            /* ---------- 基础状态 ---------- */
            const view = ref(boot.page === 'recipe' ? 'recipe' : boot.page === 'search' ? 'search' : boot.page || 'home');
            const scrolled = ref(false);
            const keyword = ref(boot.keyword || '');
            const toast = ref('');
            let toastTimer = null;

            const navItems = [
                { key: 'home', label: '发现', emoji: '🍽️' },
                { key: 'nearby', label: '附近', emoji: '📍' },
                { key: 'search', label: '找菜', emoji: '🔍' },
                { key: 'assistant', label: 'AI 助手', emoji: '🤖' },
                { key: 'profile', label: '我的', emoji: '👤' },
            ];

            const quickQueries = ['番茄 鸡蛋', '想吃点辣的', '20 分钟快手菜', '减脂餐', '下饭菜', '一人食'];
            const scenes = [
                { value: '', label: '全部', emoji: '✨' },
                { value: '早餐', label: '早餐', emoji: '🌅' },
                { value: '午餐', label: '午餐', emoji: '🍱' },
                { value: '晚餐', label: '晚餐', emoji: '🌆' },
                { value: '夜宵', label: '夜宵', emoji: '🌙' },
                { value: '减脂', label: '减脂', emoji: '🥗' },
                { value: '下饭', label: '下饭', emoji: '🍚' },
                { value: '宴客', label: '宴客', emoji: '🎉' },
                { value: '一人食', label: '一人食', emoji: '🍜' },
            ];
            const activeScene = ref('');

            const cuisines = ref(boot.cuisines || []);
            const difficulties = ref([
                { value: 1, label: '简单' }, { value: 2, label: '普通' }, { value: 3, label: '中等' },
                { value: 4, label: '较难' }, { value: 5, label: '大师' },
            ]);

            /* ---------- 用户 ---------- */
            const user = reactive({
                authenticated: !!boot.isAuthenticated,
                nickname: boot.userName || '',
                username: boot.userName || '',
                email: '',
                counts: { favorites: 0, ratings: 0 },
            });

            /* ---------- 数据 ---------- */
            const feed = ref([]);
            const feedHint = ref('基于你的口味与全站热度混合排序');
            const hot = ref([]);
            const results = ref([]);
            const detail = ref(null);
            const similar = ref([]);
            const favorites = ref([]);
            const status = reactive({ llm: { ready: !!boot.llmReady }, index: { ready: false, size: 0 } });

            const loading = reactive({ feed: false, search: false, detail: false, hot: false });

            const searchMode = ref('keyword');
            const filters = reactive({ cuisine: '', difficulty: '', max_minutes: '', sort: 'recommend' });
            const searchState = reactive({ query: '', total: 0, latency: 0, mode: 'keyword', done: false });

            /* ---------- 详情交互 ---------- */
            const myRating = ref(null);
            const showRate = ref(false);
            const commentText = ref('');
            const detailNearby = ref([]);

            /* ---------- 助手 ---------- */
            const messages = ref([]);
            const question = ref('');
            const thinking = ref(false);
            const chatLog = ref(null);
            const sessionId = ref(localStorage.getItem('xunwei.session') || uid());

            /* ---------- 偏好 ---------- */
            const dietOptions = [
                { value: 'vegetarian', label: '素食', emoji: '🥬' },
                { value: 'low_cal', label: '低卡', emoji: '⚖️' },
                { value: 'high_protein', label: '高蛋白', emoji: '💪' },
                { value: 'no_spicy', label: '不吃辣', emoji: '🧊' },
                { value: 'kid_friendly', label: '适合孩子', emoji: '🧒' },
            ];
            const pref = reactive({
                cuisines: [], diet_flags: [], spicy_level: 2, max_cook_minutes: 60, disliked_text: '',
            });
            const prefSaved = ref(false);

            /* ---------- 附近美食（独立模块） ---------- */
            const near = window.FoodNearby.createNearby(boot);

            /* ---------- 计算属性 ---------- */
            const detailEmoji = computed(() => UI.dishEmoji(detail.value && detail.value.title));

            const nutritionRows = computed(() => {
                const d = detail.value;
                if (!d || !d.nutrition) return [];
                const n = d.nutrition;
                const rows = [
                    { key: 'kcal', label: '热量', value: Math.round(n.per_serving || 0), unit: ' kcal', max: 800, color: '#fa541c' },
                    { key: 'protein', label: '蛋白质', value: Math.round((n.protein || 0) / Math.max(d.servings || 2, 1)), unit: ' g', max: 50, color: '#2f9e44' },
                    { key: 'fat', label: '脂肪', value: Math.round((n.fat || 0) / Math.max(d.servings || 2, 1)), unit: ' g', max: 50, color: '#f59f00' },
                    { key: 'carbs', label: '碳水', value: Math.round((n.carbs || 0) / Math.max(d.servings || 2, 1)), unit: ' g', max: 80, color: '#1c7ed6' },
                ];
                return rows.map((r) => ({ ...r, percent: Math.max(4, Math.min(100, (r.value / r.max) * 100)) }));
            });

            /* ---------- 提示 ---------- */
            function showToast(text, ms = 2200) {
                toast.value = text;
                clearTimeout(toastTimer);
                toastTimer = setTimeout(() => { toast.value = ''; }, ms);
            }

            /* ---------- 路由 ---------- */
            function syncHash(path) {
                if (location.hash !== '#' + path) {
                    history.replaceState(null, '', '#' + path);
                }
            }

            function go(target) {
                if (target === 'profile' && !user.authenticated) {
                    window.location.href = '/';
                    return;
                }
                view.value = target;
                syncHash('/' + target);
                window.scrollTo({ top: 0, behavior: 'smooth' });
                if (target === 'home' && !feed.value.length) loadFeed();
                if (target === 'home' && !hot.value.length) loadHot();
                if (target === 'nearby') { near.init().then(() => { near.openMap(); near.autoLocate(); }); }
                if (target === 'profile') { loadProfile(); loadFavorites(); near.loadPlaceFavorites(); near.loadMyReviews(); }
                if (target === 'assistant') scrollChat();
            }

            /* ---------- 首页数据 ---------- */
            async function loadFeed(scene) {
                loading.feed = true;
                try {
                    const params = { k: 12 };
                    if (scene) params.scene = scene;
                    const data = await API.feed(params);
                    feed.value = (data.items || []).filter((x) => x.id);
                    if (data.mode === 'personalized') feedHint.value = '综合你的口味偏好、评分与相似用户行为';
                    else if (data.mode === 'cold_start') feedHint.value = '游客推荐：热度 + 菜系多样性（登录后更懂你）';
                    else feedHint.value = '热门菜谱';
                } catch (e) {
                    showToast(e.message || '推荐加载失败');
                } finally {
                    loading.feed = false;
                }
            }

            async function loadHot() {
                loading.hot = true;
                try {
                    const data = await API.recipes({ sort: 'hot', size: 8 });
                    hot.value = data.items || [];
                } catch (e) {
                    hot.value = [];
                } finally {
                    loading.hot = false;
                }
            }

            function switchScene(value) {
                activeScene.value = value;
                loadFeed(value);
            }

            /* ---------- 搜索 ---------- */
            function doSearch(text) {
                const q = (text || keyword.value || '').trim();
                if (!q) { showToast('先输入想找的菜或食材'); return; }
                searchState.query = q;
                view.value = 'search';
                syncHash('/search');
                runSearch();
            }

            async function runSearch() {
                const q = searchState.query || keyword.value;
                if (!q) return;
                loading.search = true;
                searchState.done = false;
                try {
                    let data;
                    const started = performance.now();
                    if (searchMode.value === 'semantic') {
                        data = await API.semantic(q, 12);
                        results.value = (data.items || []).map((x) => ({ ...x, reason: x.reason || '语义相关' }));
                        searchState.mode = 'semantic';
                    } else {
                        data = await API.search(q, {
                            cuisine: filters.cuisine, difficulty: filters.difficulty,
                            max_minutes: filters.max_minutes, sort: filters.sort, size: 24,
                        });
                        results.value = data.items || [];
                        searchState.mode = 'keyword';
                    }
                    searchState.total = data.total || results.value.length;
                    searchState.latency = Math.round(data.latency_ms || (performance.now() - started));
                    searchState.done = true;
                } catch (e) {
                    showToast(e.message || '搜索失败');
                    results.value = [];
                    searchState.done = true;
                } finally {
                    loading.search = false;
                }
            }

            function setMode(mode) {
                if (searchMode.value === mode) return;
                searchMode.value = mode;
                runSearch();
            }

            function quickSearch(q) {
                keyword.value = q;
                searchMode.value = 'semantic';
                doSearch(q);
            }

            function browseCuisine(name) {
                filters.cuisine = name;
                keyword.value = '';
                searchState.query = name;
                view.value = 'search';
                syncHash('/search');
                runSearch();
            }

            function goSearchWithSort(sort) {
                filters.sort = sort;
                if (!searchState.query) searchState.query = keyword.value || '热门';
                view.value = 'search';
                syncHash('/search');
                runSearch();
            }

            /* ---------- 详情 ---------- */
            /** 菜谱详情页联查：附近有哪些店在做这道菜（复用高德 POI 搜索） */
            async function loadDishesNearby(title) {
                detailNearby.value = [];
                if (!title || !near.nb.located) return;
                try {
                    const resp = await axios.get('/api/v1/places/nearby/', {
                        params: {
                            lng: near.nb.lng,
                            lat: near.nb.lat,
                            coord: 'gcj02',
                            q: title,
                            radius: 3000,
                            sort: 'smart',
                        },
                    });
                    const body = resp.data || {};
                    if (body.code === 0 && body.data) {
                        detailNearby.value = (body.data.items || []).slice(0, 4);
                    }
                } catch (err) {
                    detailNearby.value = [];
                }
            }

            async function openRecipe(item) {
                const id = item && (item.id || item.recipe_id);
                if (!id) return;
                loading.detail = true;
                view.value = 'recipe';
                syncHash(`/recipe/${id}`);
                window.scrollTo({ top: 0, behavior: 'smooth' });
                try {
                    const data = await API.recipe(id);
                    detail.value = data;
                    myRating.value = data.my_rating || null;
                    showRate.value = false;
                    commentText.value = '';
                    similar.value = [];
                    try {
                        const sims = await API.similar(id, 4);
                        similar.value = (sims || []).filter((x) => x.id);
                    } catch (e) { /* 相似推荐失败不影响详情 */ }
                    API.track('view', id, 1);
                    loadDishesNearby(data.title);
                } catch (e) {
                    showToast(e.message || '菜谱加载失败');
                    go('home');
                } finally {
                    loading.detail = false;
                }
            }

            async function toggleFavorite() {
                if (!user.authenticated) { window.location.href = '/'; return; }
                if (!detail.value) return;
                try {
                    const data = await API.favorite(detail.value.id);
                    detail.value.is_favorite = data.is_favorite;
                    detail.value.favorite_count = data.favorite_count;
                    showToast(data.is_favorite ? '已加入收藏' : '已取消收藏');
                    if (data.is_favorite) API.track('favorite', detail.value.id, 3);
                } catch (e) {
                    showToast(e.message || '操作失败');
                }
            }

            function toggleRate() {
                if (!user.authenticated) { window.location.href = '/'; return; }
                showRate.value = !showRate.value;
            }

            async function submitRating(score) {
                if (!detail.value) return;
                try {
                    const data = await API.rate(detail.value.id, score);
                    myRating.value = score;
                    detail.value.rating_avg = data.rating_avg;
                    detail.value.rating_count = data.rating_count;
                    showRate.value = false;
                    showToast(`已评 ${score} 星`);
                    API.track('rate', detail.value.id, score);
                } catch (e) {
                    showToast(e.message || '评分失败');
                }
            }

            async function submitComment() {
                if (!detail.value || !commentText.value) return;
                try {
                    const c = await API.addComment(detail.value.id, commentText.value);
                    detail.value.comments.unshift(c);
                    commentText.value = '';
                    showToast('评论已发布');
                } catch (e) {
                    showToast(e.message || '评论失败');
                }
            }

            /* ---------- 助手 ---------- */
            function scrollChat() {
                nextTick(() => {
                    const el = chatLog.value;
                    if (el) el.scrollTop = el.scrollHeight;
                });
            }

            function askAssistant(text) {
                view.value = 'assistant';
                syncHash('/assistant');
                question.value = text;
                sendQuestion();
            }

            /** 「附近吃什么」：没定位就先定位，再带着坐标提问 */
            async function askNearby() {
                view.value = 'assistant';
                syncHash('/assistant');
                if (!near.nb.located) {
                    showToast('正在获取位置…');
                    try {
                        await near.init();
                        await near.autoLocate();
                    } catch (err) {
                        /* 定位失败也继续问，助手会引导用户去附近页 */
                    }
                }
                question.value = near.nb.located
                    ? '我在这个位置附近，走路 10 分钟内有什么好吃的？推荐 3 家并说明理由。'
                    : '附近有什么好吃的？';
                sendQuestion();
            }

            function sendQuestion() {
                const q = (question.value || '').trim();
                if (!q || thinking.value) return;
                messages.value.push({ role: 'user', content: q, citations: [] });
                const aiMsg = reactive({ role: 'assistant', content: '', citations: [] });
                messages.value.push(aiMsg);
                question.value = '';
                thinking.value = true;
                scrollChat();

                API.assistantStream(q, sessionId.value, {
                    onToken(token) {
                        if (thinking.value) thinking.value = false;
                        aiMsg.content += token;
                        scrollChat();
                    },
                    onCitations(items) {
                        aiMsg.citations = items || [];
                    },
                    onMeta(meta) {
                        if (meta && typeof meta.degraded !== 'undefined') status.llm.ready = !meta.degraded;
                    },
                    onError(err) {
                        thinking.value = false;
                        if (!aiMsg.content) aiMsg.content = `助手暂时不可用：${err.message}。\n\n可以改用上面的「找菜」按关键词检索。`;
                        scrollChat();
                    },
                    onDone() {
                        thinking.value = false;
                        scrollChat();
                    },
                }, near.nb.located ? { lng: near.nb.lng, lat: near.nb.lat } : null);
            }

            /* ---------- 用户中心 ---------- */
            async function loadProfile() {
                if (!user.authenticated) return;
                try {
                    const me = await API.me();
                    if (!me.authenticated) { user.authenticated = false; return; }
                    user.nickname = me.nickname;
                    user.username = me.username;
                    user.email = me.email;
                    user.counts = me.counts || user.counts;
                    pref.cuisines = (me.preferred_cuisines || []).map((c) => c.id);
                    pref.diet_flags = me.diet_flags || [];
                    pref.spicy_level = me.spicy_level;
                    pref.max_cook_minutes = me.max_cook_minutes;
                    pref.disliked_text = (me.disliked_ingredients || []).join(', ');
                } catch (e) { /* 忽略 */ }
            }

            async function loadFavorites() {
                if (!user.authenticated) return;
                try {
                    favorites.value = (await API.favorites()) || [];
                } catch (e) {
                    favorites.value = [];
                }
            }

            function togglePref(field, value) {
                const arr = pref[field];
                const idx = arr.indexOf(value);
                if (idx >= 0) arr.splice(idx, 1);
                else arr.push(value);
            }

            async function savePref() {
                prefSaved.value = false;
                const disliked = (pref.disliked_text || '')
                    .split(/[,，、\s]+/)
                    .map((s) => s.trim())
                    .filter(Boolean);
                try {
                    await API.saveProfile({
                        preferred_cuisines: pref.cuisines,
                        diet_flags: pref.diet_flags,
                        spicy_level: pref.spicy_level,
                        max_cook_minutes: pref.max_cook_minutes,
                        disliked_ingredients: disliked,
                    });
                    prefSaved.value = true;
                    showToast('偏好已保存');
                    loadFeed(activeScene.value);
                    setTimeout(() => { prefSaved.value = false; }, 3000);
                } catch (e) {
                    showToast(e.message || '保存失败');
                }
            }

            /* ---------- 启动 ---------- */
            onMounted(async () => {
                window.addEventListener('scroll', () => { scrolled.value = window.scrollY > 8; });
                localStorage.setItem('xunwei.session', sessionId.value);

                try {
                    const meta = await API.meta();
                    if (meta.cuisines && meta.cuisines.length) cuisines.value = meta.cuisines;
                    if (meta.difficulties) difficulties.value = meta.difficulties;
                    status.llm = meta.llm || status.llm;
                    status.index = meta.index || status.index;
                } catch (e) { /* 元数据失败不阻塞 */ }

                if (user.authenticated) { loadProfile(); }

                if (boot.recipeId) {
                    openRecipe({ id: boot.recipeId });
                } else if (view.value === 'nearby') {
                    near.init().then(() => { near.openMap(); near.autoLocate(); });
                } else if (view.value === 'search' && (boot.keyword || '')) {
                    searchState.query = boot.keyword;
                    searchMode.value = 'keyword';
                    runSearch();
                } else if (view.value === 'home') {
                    loadFeed();
                    loadHot();
                } else if (view.value === 'assistant') {
                    try {
                        const hist = await API.history(sessionId.value);
                        if (hist.messages && hist.messages.length) {
                            messages.value = hist.messages.map((m) => ({
                                role: m.role, content: m.content, citations: m.citations || [],
                            }));
                            scrollChat();
                        }
                    } catch (e) { /* 历史为空也正常 */ }
                }
            });

            return {
                /* 状态 */
                view, scrolled, keyword, toast, navItems, quickQueries, scenes, activeScene,
                cuisines, difficulties, user, feed, feedHint, hot, results, detail, similar, favorites,
                loading, searchMode, filters, searchState, status,
                myRating, showRate, commentText, detailNearby,
                messages, question, thinking, chatLog, sessionId,
                pref, prefSaved, dietOptions, nutritionRows, detailEmoji,
                /* 附近美食 */
                nb: near.nb,
                nbHasItems: near.hasItems,
                nbDistLabel: near.distLabel,
                nbInit: near.init,
                nbLocate: near.locateByGPS,
                nbLocateIp: near.locateByIP,
                nbSearch: near.search,
                nbSetRadius: near.setRadius,
                nbOnInput: near.onKeywordInput,
                nbPickTip: near.pickTip,
                nbOpenPlace: near.openPlace,
                nbCloseDetail: near.closeDetail,
                nbToggleFavorite: near.toggleFavorite,
                nbSubmitReview: near.submitReview,
                nbNavigate: near.navigate,
                nbFocus: near.focusOnMap,
                nbOpenMap: near.openMap,
                nbLoadPlaceFavorites: near.loadPlaceFavorites,
                nbLoadMyReviews: near.loadMyReviews,
                nbRemoveFavorite: near.removeFavorite,
                nbDistPercent: near.distPercent,
                nbOpenPhoto: near.openPhoto,
                nbClosePhoto: near.closePhoto,
                /* 方法 */
                go, loadFeed, loadHot, switchScene, doSearch, runSearch, setMode, quickSearch,
                browseCuisine, goSearchWithSort, openRecipe, toggleFavorite, toggleRate, submitRating,
                submitComment, askAssistant, askNearby, sendQuestion, loadProfile, loadFavorites, togglePref,
                savePref, showToast, renderMarkdown, formatTime,
                coverStyle: UI.coverStyle, dishEmoji: UI.dishEmoji,
                placeCover, placeEmoji,
            };
        },
    });

    app.mount('#app');
})();
