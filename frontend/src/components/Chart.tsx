import React, { useEffect, useRef, useState } from "react";
import { createChart, ColorType, CandlestickSeries, HistogramSeries, AreaSeries } from "lightweight-charts";

interface ChartProps {
  candles: any[];
  period: string;
}

export const Chart: React.FC<ChartProps> = ({ candles, period }) => {
  const priceChartContainerRef = useRef<HTMLDivElement>(null);
  const interestChartContainerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<any>(null);

  useEffect(() => {
    if (!priceChartContainerRef.current || !interestChartContainerRef.current || !candles || candles.length === 0) return;

    const priceContainer = priceChartContainerRef.current;
    const interestContainer = interestChartContainerRef.current;

    // Clear contents
    priceContainer.innerHTML = "";
    interestContainer.innerHTML = "";

    // 1. Create Top Chart (Price & Volume)
    const priceChart = createChart(priceContainer, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#7d8799",
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.03)" },
        horzLines: { color: "rgba(255, 255, 255, 0.03)" },
      },
      width: priceContainer.clientWidth,
      height: 250,
      timeScale: {
        visible: false, // Hide horizontal scale for the top chart
      },
      localization: {
        timeFormatter: (time: any) => {
          if (typeof time === "number") {
            const dateObj = new Date(time * 1000);
            return dateObj.toLocaleTimeString("en-IN", {
              day: "2-digit",
              month: "short",
              year: "numeric",
              hour: "2-digit",
              minute: "2-digit",
              hour12: true,
            });
          }
          return String(time);
        }
      }
    });

    // 2. Create Bottom Chart (Buyer/Seller Interest)
    const interestChart = createChart(interestContainer, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#7d8799",
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.03)" },
        horzLines: { color: "rgba(255, 255, 255, 0.03)" },
      },
      width: interestContainer.clientWidth,
      height: 120,
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.08)",
        timeVisible: period === "1D" || period === "5D",
        tickMarkFormatter: (time: any) => {
          if (typeof time === "number") {
            const dateObj = new Date(time * 1000);
            return dateObj.toLocaleTimeString("en-IN", {
              hour: "2-digit",
              minute: "2-digit",
              hour12: false,
            });
          }
          return String(time);
        }
      },
      rightPriceScale: {
        autoScale: false,
        mode: 0,
      },
      localization: {
        timeFormatter: (time: any) => {
          if (typeof time === "number") {
            const dateObj = new Date(time * 1000);
            return dateObj.toLocaleTimeString("en-IN", {
              day: "2-digit",
              month: "short",
              year: "numeric",
              hour: "2-digit",
              minute: "2-digit",
              hour12: true,
            });
          }
          return String(time);
        }
      }
    });

    // Set fixed scale for interest chart (0 to 100)
    interestChart.priceScale("right").applyOptions({
      scaleMargins: {
        top: 0.1,
        bottom: 0.1,
      },
      autoScale: false,
    });

    // Add Candlestick and Volume to Price Chart
    const candlestickSeries = priceChart.addSeries(CandlestickSeries, {
      upColor: "#3fbf87",
      downColor: "#f0736f",
      borderVisible: false,
      wickUpColor: "#3fbf87",
      wickDownColor: "#f0736f",
    });

    const volumeSeries = priceChart.addSeries(HistogramSeries, {
      color: "rgba(91, 157, 255, 0.2)",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.75,
        bottom: 0,
      },
    });

    // Add Area series for Buyer & Seller Interest to bottom chart
    const buyerSeries = interestChart.addSeries(AreaSeries, {
      lineColor: "#3fbf87",
      topColor: "rgba(63, 191, 135, 0.4)",
      bottomColor: "rgba(63, 191, 135, 0.0)",
      lineWidth: 2,
      title: "Buy Interest %",
    });

    const sellerSeries = interestChart.addSeries(AreaSeries, {
      lineColor: "#f0736f",
      topColor: "rgba(240, 115, 111, 0.25)",
      bottomColor: "rgba(240, 115, 111, 0.0)",
      lineWidth: 1.5,
      title: "Sell Interest %",
    });

    // Format and deduplicate candles
    const uniqueCandles: any[] = [];
    const seenTimes = new Set<string>();

    candles.forEach((c: any) => {
      if (c && c[0]) {
        let timeKey: string | number = c[0].substring(0, 10);
        if (period === "1D" || period === "5D") {
          // Intraday: parse full date and time, convert to Unix seconds
          const dObj = new Date(c[0]);
          if (!isNaN(dObj.getTime())) {
            timeKey = Math.floor(dObj.getTime() / 1000);
          }
        }

        const stringKey = String(timeKey);
        if (!seenTimes.has(stringKey)) {
          seenTimes.add(stringKey);
          uniqueCandles.push({ ...c, timeKey });
        }
      }
    });

    // Parse series data
    const candlestickData = uniqueCandles.map((c: any) => ({
      time: c.timeKey,
      open: Number(c[1]),
      high: Number(c[2]),
      low: Number(c[3]),
      close: Number(c[4]),
    }));

    const volumeData = uniqueCandles.map((c: any) => ({
      time: c.timeKey,
      value: Number(c[5]),
      color: Number(c[4]) >= Number(c[1]) ? "rgba(63, 191, 135, 0.25)" : "rgba(240, 115, 111, 0.25)",
    }));

    const buyerData = uniqueCandles.map((c: any) => {
      const high = Number(c[2]);
      const low = Number(c[3]);
      const close = Number(c[4]);
      const range = high - low;
      const buyPct = range > 0 ? ((close - low) / range) * 100 : 50;
      return { time: c.timeKey, value: Math.min(100, Math.max(0, buyPct)) };
    });

    const sellerData = uniqueCandles.map((c: any) => {
      const high = Number(c[2]);
      const low = Number(c[3]);
      const close = Number(c[4]);
      const range = high - low;
      const buyPct = range > 0 ? ((close - low) / range) * 100 : 50;
      const sellPct = 100 - buyPct;
      return { time: c.timeKey, value: Math.min(100, Math.max(0, sellPct)) };
    });

    candlestickSeries.setData(candlestickData);
    volumeSeries.setData(volumeData);
    buyerSeries.setData(buyerData);
    sellerSeries.setData(sellerData);

    priceChart.timeScale().fitContent();
    interestChart.timeScale().fitContent();

    // Sync Visible Ranges
    priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      interestChart.timeScale().setVisibleLogicalRange(range || null);
    });

    interestChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      priceChart.timeScale().setVisibleLogicalRange(range || null);
    });

    // Sync Crosshairs & Render Custom Tooltip
    priceChart.subscribeCrosshairMove((param) => {
      if (param.time) {
        interestChart.setCrosshairPosition(param.point ? { x: param.point.x, y: 0 } : null, param.time);
      } else {
        interestChart.setCrosshairPosition(null, null);
      }

      handleTooltipUpdate(param, candlestickSeries, priceContainer);
    });

    interestChart.subscribeCrosshairMove((param) => {
      if (param.time) {
        priceChart.setCrosshairPosition(param.point ? { x: param.point.x, y: 0 } : null, param.time);
      } else {
        priceChart.setCrosshairPosition(null, null);
      }
    });

    const handleTooltipUpdate = (param: any, series: any, containerElement: HTMLDivElement) => {
      if (
        !param.time ||
        !param.point ||
        param.point.x < 0 ||
        param.point.x > containerElement.clientWidth ||
        param.point.y < 0 ||
        param.point.y > 250
      ) {
        setTooltip(null);
        return;
      }

      const priceData = param.seriesData.get(series);
      if (!priceData) {
        setTooltip(null);
        return;
      }

      let timeString = "";
      if (typeof param.time === "number") {
        const dateObj = new Date(param.time * 1000);
        timeString = dateObj.toLocaleDateString("en-IN", {
          day: "2-digit",
          month: "short",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          hour12: true,
        });
      } else {
        const dateParts = param.time.split("-");
        if (dateParts.length === 3) {
          const dateObj = new Date(Number(dateParts[0]), Number(dateParts[1]) - 1, Number(dateParts[2]));
          timeString = dateObj.toLocaleDateString("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric",
          });
        } else {
          timeString = param.time;
        }
      }

      const ohlc = priceData as any;
      setTooltip({
        time: timeString,
        open: ohlc.open,
        high: ohlc.high,
        low: ohlc.low,
        close: ohlc.close,
        x: param.point.x,
        y: param.point.y,
      });
    };

    // Resize Handler
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !entries[0].contentRect) return;
      const { width } = entries[0].contentRect;
      priceChart.applyOptions({ width });
      interestChart.applyOptions({ width });
    });

    resizeObserver.observe(priceContainer);

    return () => {
      resizeObserver.disconnect();
      priceChart.remove();
      interestChart.remove();
    };
  }, [candles, period]);

  return (
    <div style={{ position: "relative", width: "100%", display: "flex", flexDirection: "column", gap: "8px" }}>
      {/* Price Candlestick Chart */}
      <div ref={priceChartContainerRef} style={{ width: "100%" }} />

      {/* Synced Buyer/Seller Interest Chart */}
      <div style={{ paddingLeft: "10px", paddingRight: "10px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "4px" }}>
          <span style={{ fontSize: "10px", fontWeight: "700", color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Buyer vs Seller Interest Timeline
          </span>
          <div style={{ display: "flex", gap: "12px", fontSize: "9px", fontWeight: "700" }}>
            <span style={{ color: "#3fbf87" }}>● BUY INTEREST</span>
            <span style={{ color: "#f0736f" }}>● SELL INTEREST</span>
          </div>
        </div>
        <div ref={interestChartContainerRef} style={{ width: "100%" }} />
      </div>

      {/* Floating Hover Tooltip */}
      {tooltip && (
        <div style={{
          position: "absolute",
          left: `${tooltip.x + 20}px`,
          top: `${tooltip.y + 10}px`,
          pointerEvents: "none",
          zIndex: 1000,
          padding: "10px 14px",
          backgroundColor: "rgba(11, 15, 25, 0.95)",
          border: "1px solid rgba(255, 255, 255, 0.1)",
          borderRadius: "8px",
          fontSize: "11px",
          color: "#e3e7ee",
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(4px)",
          display: "flex",
          flexDirection: "column",
          gap: "6px",
        }}>
          <span style={{ fontWeight: "800", color: "#5b9dff", borderBottom: "1px solid rgba(255,255,255,0.06)", paddingBottom: "4px", display: "block" }}>
            {tooltip.time}
          </span>
          <div style={{ display: "grid", gridTemplateColumns: "auto auto", gap: "4px 16px" }}>
            <span style={{ color: "#7d8799" }}>Open:</span>
            <span style={{ fontWeight: "700", textAlign: "right" }}>₹{tooltip.open.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
            <span style={{ color: "#7d8799" }}>High:</span>
            <span style={{ fontWeight: "700", textAlign: "right", color: "#52d69a" }}>₹{tooltip.high.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
            <span style={{ color: "#7d8799" }}>Low:</span>
            <span style={{ fontWeight: "700", textAlign: "right", color: "#ff8a86" }}>₹{tooltip.low.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
            <span style={{ color: "#7d8799" }}>Close:</span>
            <span style={{ fontWeight: "700", textAlign: "right" }}>₹{tooltip.close.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
          </div>
        </div>
      )}
    </div>
  );
};
