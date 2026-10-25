/* ===========================================================
   寻味 · API 客户端
   统一处理 {code, msg, data} 结构；SSE 用 fetch 流式读取。
   =========================================================== */
(function () {
    'use strict';

    const BASE = '/api/v1';

    const http = axios.create({
        baseURL: BASE,
        timeout: 20000,
        withCredentials: true,
        headers: { 'Content-Type': 'application/json' },
    });

    /** 拆包：code !== 0 视为业务失败 */
    async function unwrap(promise) {
        try {
            const resp = await promise;
            const body = resp.data || {};
            if (body.code === 0) return body.data;
            const err = new Error(body.msg || '请求失败');
            err.code = body.code;
            err.status = resp.status;
            throw err;
        } catch (e) {
            if (e.response) {
                const body = e.response.data || {};
                const err = new Error(body.msg || `请求失败（${e.response.status}）`);
                err.code = body.code;
                err.status = e.response.status;
                throw err;
            }
            throw e;
        }
    }

    function qs(params) {
        const usable = Object.entries(params || {}).filter(
            ([, v]) => v !== '' && v !== null && v !== undefined
        );
        return usable.length ? '?' + usable.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&') : '';
    }

    const API = {
        /* 元数据 */
        meta: () => unwrap(http.get('/meta/')),
        health: () => unwrap(http.get('/health/')),

        /* 认证 */
        me: () => unwrap(http.get('/auth/me/')),
        sendCode: (email) => unwrap(http.post('/auth/send_code/', { email })),
        login: (email, code) => unwrap(http.post('/auth/login/', { email, code })),
        logout: () => unwrap(http.post('/auth/logout/')),

        /* 菜谱 */
        recipes: (params) => unwrap(http.get('/recipes/' + qs(params))),
        recipe: (id) => unwrap(http.get(`/recipes/${id}/`)),
        rate: (id, score) => unwrap(http.post(`/recipes/${id}/rate/`, { score })),
        favorite: (id) => unwrap(http.post(`/recipes/${id}/favorite/`)),
        comments: (id) => unwrap(http.get(`/recipes/${id}/comments/`)),
        addComment: (id, content, parentId) =>
            unwrap(http.post(`/recipes/${id}/comments/`, { content, parent_id: parentId || 0 })),
        similar: (id, k) => unwrap(http.get(`/recipes/${id}/similar/` + qs({ k }))),

        /* 检索与推荐 */
        search: (q, params) => unwrap(http.get('/search/' + qs({ q, ...(params || {}) }))),
        semantic: (q, k) => unwrap(http.get('/search/semantic/' + qs({ q, k }))),
        feed: (params) => unwrap(http.get('/feed/' + qs(params || {}))),

        /* 助手 */
        history: (sessionId) => unwrap(http.get('/assistant/history/' + qs({ session_id: sessionId }))),
        assistantDebug: (q, k) => unwrap(http.get('/assistant/debug/' + qs({ q, k }))),
        assistantSync: (question, sessionId, useTools) =>
            unwrap(http.post('/assistant/chat/sync/', { question, session_id: sessionId, use_tools: !!useTools })),

        /** SSE 流式对话：返回 abort 函数。location={lng,lat} 用于「附近吃什么」 */
        assistantStream(question, sessionId, handlers, location) {
            const controller = new AbortController();
            const { onToken, onCitations, onMeta, onDone, onError } = handlers || {};

            fetch(`${BASE}/assistant/chat/`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question,
                    session_id: sessionId,
                    use_tools: true,
                    lng: (location && location.lng) || 0,
                    lat: (location && location.lat) || 0,
                }),
                signal: controller.signal,
            })
                .then(async (resp) => {
                    if (!resp.ok || !resp.body) throw new Error(`服务返回 ${resp.status}`);
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder('utf-8');
                    let buffer = '';
                    for (;;) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        const parts = buffer.split('\n\n');
                        buffer = parts.pop();
                        for (const part of parts) {
                            const line = part.trim();
                            if (!line.startsWith('data:')) continue;
                            const raw = line.slice(5).trim();
                            if (!raw || raw === '[DONE]') continue;
                            let evt;
                            try { evt = JSON.parse(raw); } catch (e) { continue; }
                            if (evt.type === 'token' && onToken) onToken(evt.text || '');
                            else if (evt.type === 'citations' && onCitations) onCitations(evt.items || []);
                            else if (evt.type === 'meta' && onMeta) onMeta(evt);
                            else if (evt.type === 'error' && onError) onError(new Error(evt.message || '生成失败'));
                            else if (evt.type === 'done' && onDone) onDone(evt);
                        }
                    }
                    if (onDone) onDone({});
                })
                .catch((err) => {
                    if (err.name === 'AbortError') return;
                    if (onError) onError(err);
                });

            return () => controller.abort();
        },

        /* 网络搜索 */
        websearch: (q, limit) => unwrap(http.get('/websearch/' + qs({ q, limit }))),
        importRecipe: (url) => unwrap(http.post('/websearch/import/', { url })),

        /* 用户 */
        profile: () => unwrap(http.get('/profile/')),
        saveProfile: (data) => unwrap(http.patch('/profile/', data)),
        favorites: () => unwrap(http.get('/favorites/')),
        track: (action, recipeId, weight) =>
            http.post('/interactions/', { action, recipe_id: recipeId || 0, weight: weight || 1 }).catch(() => null),

        /* 运维 */
        rebuildIndex: () => unwrap(http.post('/admin/rebuild_index/')),
    };

    window.FoodAPI = API;
})();
