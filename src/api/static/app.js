/**
 * Mana Market Engine - Daily Report Frontend Controller
 * Powered by Alpine.js and Chart.js
 */

// MTG format metadata: legality key (sent to API), display label, and date range info.
const FORMATS = [
    { key: 'standard', label: 'Standard',   icon: '⭐', range: 'WOE → Present' },
    { key: 'pioneer',  label: 'Pioneer',    icon: '🏔️', range: 'RTR → Present' },
    { key: 'modern',   label: 'Modern',     icon: '⚙️', range: 'MRD → Present' },
];

document.addEventListener('alpine:init', () => {
    Alpine.data('dashboardApp', () => ({
        // Format State
        formats: FORMATS,
        activeFormat: 'standard',

        // Movers State
        direction: 'gainers',
        days: 1,
        limit: 15,
        loading: false,
        movers: [],
        watchMovers: [],
        watchLoading: false,
        showWatch: true,
        sortByVelocity: false,
        renderKey: 0,

        // Summary Highlight State
        topGainer: null,
        topLoser: null,
        summaryLoading: false,
        lastUpdatedTime: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),

        // Search State
        searchQuery: '',
        searchResults: [],
        searchLoading: false,
        showSearchDropdown: false,
        searchDebounceTimeout: null,

        // Modal / Card Details State
        isModalOpen: false,
        historyLoading: false,
        selectedCard: null,
        cardStats: null,
        cardHistory: [],
        historyDays: 30,
        chartInstance: null,

        async init() {
            await Promise.all([
                this.fetchMovers(),
                this.fetchSummaryHighlights()
            ]);

            document.addEventListener('click', (e) => {
                if (!e.target.closest('#search-container')) {
                    this.showSearchDropdown = false;
                }
            });

            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && this.isModalOpen) this.closeModal();
            });
        },

        // --- Format ---
        setFormat(key) {
            if (this.activeFormat === key) return;
            this.activeFormat = key;
            this.fetchMovers();
            this.fetchSummaryHighlights();
        },

        get activeFormatMeta() {
            return FORMATS.find(f => f.key === this.activeFormat) || FORMATS[0];
        },

        // --- Data Fetching ---
        moversUrl(direction, limit, tier = 'confirmed') {
            return `/api/v1/movers?direction=${direction}&days=${this.days}&limit=${limit}&format=${this.activeFormat}&tier=${tier}`;
        },

        async fetchMovers() {
            this.loading = true;
            this.watchLoading = true;
            try {
                const [moversResp, watchResp] = await Promise.all([
                    fetch(this.moversUrl(this.direction, this.limit, 'confirmed')),
                    fetch(this.moversUrl(this.direction, this.limit, 'watch'))
                ]);
                if (moversResp.ok) {
                    const data = await moversResp.json();
                    this.movers = data.results || [];
                }
                if (watchResp.ok) {
                    const data = await watchResp.json();
                    this.watchMovers = data.results || [];
                }
                this.lastUpdatedTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch (err) {
                console.error("Failed to fetch market movers:", err);
                this.movers = [];
                this.watchMovers = [];
            } finally {
                this.loading = false;
                this.watchLoading = false;
            }
        },

        toggleVelocitySort() {
            console.log('toggle called, sortByVelocity:', this.sortByVelocity, 'movers count:', this.movers.length);
            const sortFn = this.sortByVelocity
                ? (a, b) => Math.abs(parseFloat(b.velocity || 0)) - Math.abs(parseFloat(a.velocity || 0))
                : (a, b) => Math.abs(parseFloat(b.percent_change || 0)) - Math.abs(parseFloat(a.percent_change || 0));
            this.movers = [...this.movers].sort(sortFn);
            this.watchMovers = [...this.watchMovers].sort(sortFn);
        },

        async fetchSummaryHighlights() {
            this.summaryLoading = true;
            try {
                const [gainersResp, losersResp] = await Promise.all([
                    fetch(this.moversUrl('gainers', 1)),
                    fetch(this.moversUrl('losers', 1))
                ]);
                if (gainersResp.ok) {
                    const gData = await gainersResp.json();
                    this.topGainer = gData.results && gData.results.length ? gData.results[0] : null;
                }
                if (losersResp.ok) {
                    const lData = await losersResp.json();
                    this.topLoser = lData.results && lData.results.length ? lData.results[0] : null;
                }
            } catch (err) {
                console.error("Failed to load highlights:", err);
            } finally {
                this.summaryLoading = false;
            }
        },

        setDirection(newDirection) {
            if (this.direction === newDirection) return;
            this.direction = newDirection;
            this.fetchMovers();
        },

        setDays(newDays) {
            if (this.days === newDays) return;
            this.days = newDays;
            this.fetchMovers();
            this.fetchSummaryHighlights();
        },

        setLimit(newLimit) {
            if (this.limit === newLimit) return;
            this.limit = newLimit;
            this.fetchMovers();
        },

        // --- Card Search ---
        onSearchInput() {
            clearTimeout(this.searchDebounceTimeout);
            const query = this.searchQuery.trim();
            if (query.length < 2) {
                this.searchResults = [];
                this.showSearchDropdown = false;
                return;
            }
            this.searchDebounceTimeout = setTimeout(async () => {
                this.searchLoading = true;
                this.showSearchDropdown = true;
                try {
                    const resp = await fetch(`/api/v1/cards/search?q=${encodeURIComponent(query)}&limit=6`);
                    if (resp.ok) {
                        const data = await resp.json();
                        this.searchResults = data.results || [];
                    }
                } catch (e) {
                    console.error("Search failed:", e);
                } finally {
                    this.searchLoading = false;
                }
            }, 250);
        },

        selectCard(card) {
            this.showSearchDropdown = false;
            this.searchQuery = '';
            this.inspectCardById(card.card_id, card);
        },

        // --- Card Details & Chart Inspection ---
        async inspectCardById(cardId, initialCardData = null) {
            if (!cardId) return;
            this.isModalOpen = true;
            this.historyLoading = true;
            this.selectedCard = initialCardData;
            this.cardStats = null;
            this.cardHistory = [];

            try {
                const resp = await fetch(`/api/v1/cards/${cardId}/history?days=${this.historyDays}`);
                if (!resp.ok) throw new Error("Failed to load history");
                const data = await resp.json();
                this.selectedCard = data.card || initialCardData;
                this.cardStats = data.stats;
                this.cardHistory = data.history || [];
                this.renderPriceChart(this.cardHistory);
            } catch (err) {
                console.error("Failed to load card history:", err);
            } finally {
                this.historyLoading = false;
            }
        },

        setHistoryDays(days) {
            this.historyDays = days;
            if (this.selectedCard && this.selectedCard.card_id) {
                this.inspectCardById(this.selectedCard.card_id, this.selectedCard);
            }
        },

        renderPriceChart(history) {
            const canvas = document.getElementById('priceHistoryChart');
            if (!canvas) return;

            const validPoints = history.filter(h => h.price_eur !== null && h.price_eur !== undefined);
            if (validPoints.length === 0) return;

            const labels = validPoints.map(p => {
                const d = new Date(p.timestamp);
                return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            });
            const dataValues = validPoints.map(p => parseFloat(p.price_eur));

            const isPositive = dataValues.length > 1 && dataValues[dataValues.length - 1] >= dataValues[0];
            const lineColor = isPositive ? '#10b981' : '#f43f5e';
            const bgGradientColor = isPositive ? 'rgba(16, 185, 129, 0.15)' : 'rgba(244, 63, 94, 0.15)';

            if (this.chartInstance) {
                this.chartInstance.data.labels = labels;
                this.chartInstance.data.datasets[0].data = dataValues;
                this.chartInstance.options.plugins.tooltips.callbacks.label = (ctx) => `€${parseFloat(ctx.parsed.y).toFixed(2)} EUR`;
                this.chartInstance.options.scales.y.ticks.callback = (val) => `€${parseFloat(val).toFixed(2)}`;
                this.chartInstance.update();
            } else {
                const ctx = canvas.getContext('2d');
                const gradient = ctx.createLinearGradient(0, 0, 0, 240);
                gradient.addColorStop(0, bgGradientColor);
                gradient.addColorStop(1, 'rgba(15, 23, 42, 0)');

                this.chartInstance = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels,
                        datasets: [{
                            label: 'Market Price (EUR)',
                            data: dataValues,
                            borderColor: lineColor,
                            backgroundColor: gradient,
                            borderWidth: 2.5,
                            fill: true,
                            tension: 0.35,
                            pointRadius: dataValues.length > 20 ? 1.5 : 3.5,
                            pointHoverRadius: 6,
                            pointBackgroundColor: lineColor,
                            pointBorderColor: '#0f172a',
                            pointBorderWidth: 2
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { intersect: false, mode: 'index' },
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                backgroundColor: '#0f172a',
                                titleColor: '#94a3b8',
                                bodyColor: '#f8fafc',
                                borderColor: '#334155',
                                borderWidth: 1,
                                padding: 10,
                                displayColors: false,
                                callbacks: {
                                    label: (ctx) => `€${parseFloat(ctx.parsed.y).toFixed(2)} EUR`
                                }
                            }
                        },
                        scales: {
                            x: {
                                grid: { color: 'rgba(51, 65, 85, 0.3)', drawBorder: false },
                                ticks: { color: '#64748b', font: { size: 11 } }
                            },
                            y: {
                                grid: { color: 'rgba(51, 65, 85, 0.3)', drawBorder: false },
                                ticks: {
                                    color: '#64748b',
                                    font: { size: 11 },
                                    callback: (val) => `€${parseFloat(val).toFixed(2)}`
                                }
                            }
                        }
                    }
                });
            }
        },

        closeModal() {
            this.isModalOpen = false;
            if (this.chartInstance) {
                this.chartInstance.destroy();
                this.chartInstance = null;
            }
        },

        // --- Formatting Helpers ---
        formatPrice(val) {
            if (val === null || val === undefined || isNaN(val)) return 'N/A';
            return `€${parseFloat(val).toFixed(2)}`;
        },

        formatDelta(val) {
            if (val === null || val === undefined || isNaN(val)) return 'N/A';
            const num = parseFloat(val);
            return `${num > 0 ? '+' : ''}€${num.toFixed(2)}`;
        },

        formatPercent(val) {
            if (val === null || val === undefined || isNaN(val)) return '0.0%';
            const num = parseFloat(val);
            return `${num > 0 ? '+' : ''}${num.toFixed(1)}%`;
        },

        formatOracleText(text) {
            if (!text) return '';
            return text.replace(/\n/g, '<br>');
        },

        getRarityBadgeClass(rarity) {
            switch ((rarity || '').toLowerCase()) {
                case 'mythic':   return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
                case 'rare':     return 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20';
                case 'uncommon': return 'bg-sky-500/10 text-sky-400 border-sky-500/20';
                default:         return 'bg-slate-700/30 text-slate-400 border-slate-700/50';
            }
        }
    }));
});

