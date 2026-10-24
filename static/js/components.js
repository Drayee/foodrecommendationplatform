/* ===========================================================
   寻味 · 通用组件
   RecipeCard / StarRating —— 以全局组件注册，供 app.js 使用。
   =========================================================== */
(function () {
    'use strict';

    const { defineComponent, h, computed } = Vue;

    /* ---------------- 菜名 → emoji（无封面时做视觉占位） ---------------- */
    const EMOJI_RULES = [
        [/鱼|鲈|鲫|虾|蟹|贝|蛤|鱿|海鲜|三文鱼|鳕/, '🐟'],
        [/鸡|鸭|鹅|翅|腿|卤/, '🍗'],
        [/牛|羊|猪|肉|排骨|肘|里脊|腩/, '🥩'],
        [/豆腐|豆干|腐竹|豆皮|素/, '🫘'],
        [/面|粉|米线|饺子|馄饨|包|饼|馒头/, '🍜'],
        [/饭|粥|煲仔|炒饭|拌饭/, '🍚'],
        [/汤|羹|炖|煲/, '🍲'],
        [/菜|蔬|西兰花|菠菜|生菜|白菜|黄瓜|茄子|土豆|番茄|萝卜/, '🥬'],
        [/蛋糕|甜|布丁|奶|芝士|冰|糖水|银耳/, '🍰'],
        [/蛋/, '🍳'],
        [/咖啡|茶|饮|果汁/, '🥤'],
    ];

    function dishEmoji(title) {
        const name = title || '';
        for (const [re, emoji] of EMOJI_RULES) {
            if (re.test(name)) return emoji;
        }
        return '🍽️';
    }

    /* ---------------- 稳定渐变（按名字 hash，同一道菜颜色固定） ---------------- */
    const PALETTES = [
        ['#ff9a6c', '#ff6b35'],
        ['#ffc46b', '#ff9f43'],
        ['#f7a072', '#e8590c'],
        ['#ffb3a7', '#f03e3e'],
        ['#ffd6a5', '#f4a261'],
        ['#c1e1c1', '#2f9e44'],
        ['#b8d8f9', '#1c7ed6'],
        ['#e5c6ff', '#7048e8'],
        ['#ffe066', '#f59f00'],
        ['#ffc9de', '#e64980'],
    ];

    function hashOf(str) {
        let h = 0;
        for (let i = 0; i < (str || '').length; i += 1) {
            h = (h * 31 + str.charCodeAt(i)) % 1000000007;
        }
        return h;
    }

    function coverStyle(item) {
        const seed = hashOf((item && (item.title || String(item.id))) || 'dish');
        const pair = PALETTES[seed % PALETTES.length];
        if (item && item.cover_url) {
            return {
                backgroundImage: `url("${item.cover_url}")`,
                backgroundSize: 'cover',
                backgroundPosition: 'center',
            };
        }
        return { backgroundImage: `linear-gradient(135deg, ${pair[0]}, ${pair[1]})` };
    }

    /* ---------------- 星级 ---------------- */
    const StarRating = defineComponent({
        name: 'StarRating',
        props: {
            value: { type: Number, default: 0 },
            readonly: { type: Boolean, default: false },
            size: { type: Number, default: 16 },
        },
        emits: ['change'],
        setup(props, { emit }) {
            const stars = computed(() => {
                const v = Number(props.value) || 0;
                return [1, 2, 3, 4, 5].map((n) => ({
                    n,
                    state: v >= n - 0.25 ? 'full' : v >= n - 0.75 ? 'half' : 'empty',
                }));
            });
            const pick = (n) => {
                if (props.readonly) return;
                emit('change', n);
            };
            return () =>
                h(
                    'span',
                    { class: 'star-rating', style: { fontSize: props.size + 'px' } },
                    stars.value.map((s) =>
                        h(
                            'span',
                            {
                                key: s.n,
                                class: ['star', s.state, props.readonly ? 'readonly' : 'clickable'],
                                onClick: () => pick(s.n),
                                title: props.readonly ? '' : `评 ${s.n} 星`,
                            },
                            s.state === 'full' ? '★' : s.state === 'half' ? '⯨' : '☆'
                        )
                    )
                );
        },
    });

    /* ---------------- 菜谱卡片 ---------------- */
    const RecipeCard = defineComponent({
        name: 'RecipeCard',
        props: {
            item: { type: Object, required: true },
            reason: { type: String, default: '' },
            score: { type: Number, default: null },
        },
        emits: ['open'],
        setup(props, { emit }) {
            return () =>
                h(
                    'article',
                    {
                        class: 'recipe-card',
                        onClick: () => emit('open', props.item),
                        role: 'button',
                        tabindex: 0,
                    },
                    [
                        h('div', { class: 'card-cover', style: coverStyle(props.item) }, [
                            h('span', { class: 'cover-emoji' }, dishEmoji(props.item.title)),
                            h('span', { class: 'cover-badge' }, props.item.cuisine || '家常'),
                            props.reason
                                ? h('span', { class: 'cover-reason', title: props.reason }, props.reason)
                                : null,
                        ]),
                        h('div', { class: 'card-body' }, [
                            h('h3', { class: 'card-title' }, props.item.title),
                            h('p', { class: 'card-summary' }, props.item.summary || ''),
                            h('div', { class: 'card-meta' }, [
                                h('span', { class: 'm' }, `⏱ ${props.item.total_minutes} 分钟`),
                                h('span', { class: 'm' }, `🔥 ${props.item.calories} kcal`),
                                h('span', { class: 'm' }, props.item.difficulty_label || ''),
                            ]),
                            h('div', { class: 'card-foot' }, [
                                h('span', { class: 'rating' }, [
                                    h('b', {}, '★'),
                                    ` ${(props.item.rating_avg || 0).toFixed ? props.item.rating_avg.toFixed(1) : props.item.rating_avg}`,
                                    h('small', {}, ` (${props.item.rating_count || 0})`),
                                ]),
                                h('span', { class: 'fav' }, `♥ ${props.item.favorite_count || 0}`),
                                props.score !== null && props.score !== undefined && props.score > 0
                                    ? h('span', { class: 'score' }, `匹配 ${(props.score * 100).toFixed(0)}%`)
                                    : null,
                            ]),
                        ]),
                    ]
                );
        },
    });

    /* ---------------- 导出 ---------------- */
    window.FoodUI = { dishEmoji, coverStyle, StarRating, RecipeCard };
})();
