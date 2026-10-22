/**
 * Mana Market Engine - Daily Report Frontend Controller
 * Powered by Alpine.js and Chart.js
 */

document.addEventListener('alpine:init', () => {
    Alpine.data('dashboardApp', () => ({
        // State
        direction: 'gainers',
        days: 1,
        limit: 15,
        loading: false,
        movers: [],
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

            // Close search dropdown on click outside
            document.addEventListener('click', (e) => {
                if (!e.target.closest('#search-container')) {
                    this.showSearchDropdown = false;
                }
            });

            // Close modal on Escape
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && this.isModalOpen) {
                    this.closeModal();
                }
            });
        },

        // --- Data Fetching ---
        async fetchMovers() {
            this.loading = true;
            try {
                const resp = await fetch(`/api/v1/movers?direction=${this.direction}&days=${this.days}&limit=${this.limit}`);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                this.movers = data.results || [];
                this.lastUpdatedTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch (err) {
                console.error("Failed to fetch market movers:", err);
                this.movers = [];
            } finally {
                this.loading = false;
            }
        },

        async fetchSummaryHighlights() {
            this.summaryLoading = true;
            try {
                const [gainersResp, losersResp] = await Promise.all([
                    fetch(`/api/v1/movers?direction=gainers&days=${this.days}&limit=1`),
                    fetch(`/api/v1/movers?direction=losers&days=${this.days}&limit=1`)
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

                // Render Chart on next DOM tick once modal is visible
                this.$nextTick(() => {
                    this.renderPriceChart(this.cardHistory);
                });
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

            if (this.chartInstance) {
                this.chartInstance.destroy();
                this.chartInstance = null;
            }

            const validPoints = history.filter(h => h.price_usd !== null && h.price_usd !== undefined);
            if (validPoints.length === 0) return;

            const labels = validPoints.map(p => {
                const d = new Date(p.timestamp);
                return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            });
            const dataValues = validPoints.map(p => parseFloat(p.price_usd));

            const isPositive = dataValues.length > 1 && dataValues[dataValues.length - 1] >= dataValues[0];
            const lineColor = isPositive ? '#10b981' : '#f43f5e'; // emerald-500 vs rose-500
            const bgGradientColor = isPositive ? 'rgba(16, 185, 129, 0.15)' : 'rgba(244, 63, 94, 0.15)';

            const ctx = canvas.getContext('2d');
            const gradient = ctx.createLinearGradient(0, 0, 0, 240);
            gradient.addColorStop(0, bgGradientColor);
            gradient.addColorStop(1, 'rgba(15, 23, 42, 0)');

            this.chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Market Price (USD)',
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
                    interaction: {
                        intersect: false,
                        mode: 'index'
                    },
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
                                label: (ctx) => `$${parseFloat(ctx.parsed.y).toFixed(2)} USD`
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
                                callback: (val) => `$${parseFloat(val).toFixed(2)}`
                            }
                        }
                    }
                }
            });
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
            return `$${parseFloat(val).toFixed(2)}`;
        },

        formatDelta(val) {
            if (val === null || val === undefined || isNaN(val)) return 'N/A';
            const num = parseFloat(val);
            const prefix = num > 0 ? '+' : '';
            return `${prefix}$${num.toFixed(2)}`;
        },

        formatPercent(val) {
            if (val === null || val === undefined || isNaN(val)) return '0.0%';
            const num = parseFloat(val);
            const prefix = num > 0 ? '+' : '';
            return `${prefix}${num.toFixed(1)}%`;
        },

        getRarityBadgeClass(rarity) {
            const r = (rarity || '').toLowerCase();
            switch (r) {
                case 'mythic':
                    return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
                case 'rare':
                    return 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20';
                case 'uncommon':
                    return 'bg-sky-500/10 text-sky-400 border-sky-500/20';
                default:
                    return 'bg-slate-700/30 text-slate-400 border-slate-700/50';
            }
        }
    }));
});

