/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — DASHBOARD MODULE
   ═══════════════════════════════════════════════════════════════════════════════════
   Handles: Sidebar navigation, Chart.js charts, server status, metrics
   Communicates with app.js via CustomEvents (skyclaw-telemetry, skyclaw-message)
   Dependencies: Chart.js (loaded from CDN before this script)
═══════════════════════════════════════════════════════════════════════════════════ */

(function () {
    'use strict';

    // ═══════════════════════════════════════════════════════════════════
    // 1. CHART.JS GLOBAL CONFIGURATION
    // ═══════════════════════════════════════════════════════════════════

    if (window.Chart) {
        Chart.defaults.color = '#d4c4a8';
        Chart.defaults.borderColor = 'rgba(62, 39, 35, 0.5)';
        Chart.defaults.font.family = "'MedievalSharp', cursive";
        Chart.defaults.maintainAspectRatio = false;
        // Disable default plugins animations for reduced-motion
        Chart.defaults.animation.duration = 400;
    }

    // ═══════════════════════════════════════════════════════════════════
    // 2. SIDEBAR NAVIGATION
    // ═══════════════════════════════════════════════════════════════════

    const sidebarNav = document.querySelector('.sidebar-nav');
    if (sidebarNav) {
        sidebarNav.addEventListener('click', function (e) {
            var link = e.target.closest('a');
            if (!link) return;
            e.preventDefault();

            // Toggle active state + aria-current (C02)
            sidebarNav.querySelectorAll('a').forEach(function (a) {
                a.classList.remove('active');
                a.removeAttribute('aria-current');
            });
            link.classList.add('active');
            link.setAttribute('aria-current', 'page');
        });
    }

    // ═══════════════════════════════════════════════════════════════════
    // 3. PERFORMANCE LINE CHART
    // ═══════════════════════════════════════════════════════════════════

    const MAX_DATA_POINTS = 60;  // 60 segundos a 1 Hz
    let performanceChart = null;

    function initPerformanceChart() {
        const canvas = document.getElementById('performance-chart');
        if (!canvas || !window.Chart) return;

        const ctx = canvas.getContext('2d');

        // Start clean — real data arrives via skyclaw-telemetry events
        const labels = [];
        const data = [];

        // Gold gradient fill
        var gradient = ctx.createLinearGradient(0, 0, 0, 200);
        gradient.addColorStop(0, 'rgba(255, 179, 0, 0.25)');
        gradient.addColorStop(1, 'rgba(255, 179, 0, 0.02)');

        performanceChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'CPU %',
                    data: data,
                    borderColor: '#ffb300',
                    backgroundColor: gradient,
                    borderWidth: 2,
                    fill: true,
                    tension: 0.3,
                    // Diamond-shaped nodes
                    pointStyle: 'rectRot',
                    pointRadius: 5,
                    pointBackgroundColor: '#ffb300',
                    pointBorderColor: 'rgba(255, 170, 0, 0.6)',
                    pointBorderWidth: 3,
                    pointHoverRadius: 8,
                    pointHoverBackgroundColor: '#ffb300',
                    pointHoverBorderColor: 'rgba(255, 170, 0, 0.8)',
                    pointHoverBorderWidth: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(62, 39, 35, 0.9)',
                        titleColor: '#ffb300',
                        bodyColor: '#d4c4a8',
                        borderColor: '#ffb300',
                        borderWidth: 1,
                        cornerRadius: 4,
                        padding: 10
                    }
                },
                scales: {
                    x: {
                        display: false
                    },
                    y: {
                        beginAtZero: true,
                        max: 200,
                        ticks: {
                            stepSize: 40,
                            color: '#8a7a64',
                            font: { size: 10 }
                        },
                        grid: {
                            color: 'rgba(62, 39, 35, 0.4)',
                            lineWidth: 1
                        }
                    }
                },
                interaction: {
                    intersect: false,
                    mode: 'index'
                }
            }
        });
    }

    // ═══════════════════════════════════════════════════════════════════
    // 4. BAR CHART WITH RUNIC LABELS
    // ═══════════════════════════════════════════════════════════════════

    let barChart = null;

    function initBarChart() {
        var canvas = document.getElementById('bar-chart');
        if (!canvas || !window.Chart) return;

        var ctx = canvas.getContext('2d');

        // Elder Futhark runes as labels
        var runeLabels = ['ᚠ', 'ᚢ', 'ᚦ', 'ᚨ', 'ᛗ', 'ᚱ', 'ᛚ', 'ᛏ', 'ᚲ', 'ᛊ'];
        // Start zeroed — awaiting live module telemetry
        var barData = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];

        // Gold gradient for bars
        var barGradient = ctx.createLinearGradient(0, 0, 0, 200);
        barGradient.addColorStop(0, '#ffb300');
        barGradient.addColorStop(0.6, '#c8860a');
        barGradient.addColorStop(1, '#8b6508');

        barChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: runeLabels,
                datasets: [{
                    label: 'Rendimiento',
                    data: barData,
                    backgroundColor: barGradient,
                    borderColor: 'rgba(255, 179, 0, 0.4)',
                    borderWidth: 1,
                    borderRadius: 3,
                    hoverBackgroundColor: '#ffb300',
                    hoverBorderColor: '#ffb300',
                    hoverBorderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(62, 39, 35, 0.9)',
                        titleColor: '#ffb300',
                        bodyColor: '#d4c4a8',
                        borderColor: '#ffb300',
                        borderWidth: 1,
                        cornerRadius: 4,
                        titleFont: { family: "'Noto Sans Runic', 'Segoe UI Historic', sans-serif" }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#ffb300',
                            font: {
                                family: "'Noto Sans Runic', 'Segoe UI Historic', sans-serif",
                                size: 16
                            }
                        },
                        grid: {
                            display: false
                        }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            display: false
                        },
                        grid: {
                            color: 'rgba(62, 39, 35, 0.3)',
                            lineWidth: 1
                        }
                    }
                }
            }
        });
    }

    // ═══════════════════════════════════════════════════════════════════
    // 5. TELEMETRY EVENT LISTENER (from app.js)
    // ═══════════════════════════════════════════════════════════════════

    window.addEventListener('skyclaw-telemetry', function (e) {
        var stats = e.detail;
        if (!stats) return;

        // Update performance chart with rolling window
        if (performanceChart && stats.cpu !== undefined) {
            var chart = performanceChart;
            var dataset = chart.data.datasets[0];

            dataset.data.push(parseFloat(stats.cpu) || 0);
            chart.data.labels.push('');

            // Rolling window: shift old data
            if (dataset.data.length > MAX_DATA_POINTS) {
                dataset.data.shift();
                chart.data.labels.shift();
            }

            chart.update('none'); // Skip animation for real-time updates
        }

        // Update metric card
        var metricValue = document.getElementById('metric-value');
        if (metricValue && stats.ram !== undefined) {
            metricValue.textContent = stats.ram;
        }
    });

    // ═══════════════════════════════════════════════════════════════════
    // 6. MESSAGE EVENT LISTENER (from app.js)
    // ═══════════════════════════════════════════════════════════════════

    window.addEventListener('skyclaw-message', function (e) {
        var data = e.detail;
        if (!data) return;

        // Future: update server status cards based on incoming data
        // For now, server cards show static placeholder data
    });

    // ═══════════════════════════════════════════════════════════════════
    // 7. INITIALIZATION
    // ═══════════════════════════════════════════════════════════════════

    document.addEventListener('DOMContentLoaded', function () {
        initPerformanceChart();
        initBarChart();
    });

})();
