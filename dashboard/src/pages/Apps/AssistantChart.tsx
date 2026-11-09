import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { AppAssistantChart } from "../../api/modules/apps";

const COLORS = [
  "#1677ff",
  "#52c41a",
  "#faad14",
  "#eb2f96",
  "#722ed1",
  "#13c2c2",
  "#fa541c",
  "#a0d911",
];

function shortNumber(value: number): string {
  return Math.abs(value) >= 10_000
    ? `${Math.round(value / 1000)}k`
    : `${value}`;
}

/** One chart returned by the app assistant. */
export default function AssistantChart({
  chart,
}: {
  chart: AppAssistantChart;
}) {
  const { t } = useTranslation();
  const series = Array.isArray(chart.series) ? chart.series : [];
  const data = Array.isArray(chart.data) ? chart.data : [];

  if (!series.length || !data.length) {
    return (
      <div style={{ fontSize: 12, opacity: 0.65 }}>
        {t("apps.assistantChartEmpty", {
          defaultValue: "No chartable data",
        })}
      </div>
    );
  }

  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="rgba(0, 0, 0, 0.08)" />
      <XAxis
        dataKey="x"
        tick={{ fontSize: 11 }}
        interval="preserveStartEnd"
        minTickGap={12}
        height={36}
        tickFormatter={(value: string) =>
          value.length > 12 ? `${value.slice(0, 11)}…` : value
        }
      />
      <YAxis tick={{ fontSize: 11 }} width={48} tickFormatter={shortNumber} />
      <Tooltip />
      {series.length > 1 ? (
        <Legend
          verticalAlign="bottom"
          height={28}
          wrapperStyle={{ fontSize: 12 }}
        />
      ) : null}
    </>
  );

  let body: ReactNode;
  if (chart.type === "scatter") {
    const yKey = series[0].key;
    body = (
      <ScatterChart
        data={data}
        margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(0, 0, 0, 0.08)" />
        <XAxis
          type="number"
          dataKey="x"
          name={chart.x_label || chart.x}
          tick={{ fontSize: 11 }}
          height={36}
          tickFormatter={shortNumber}
        />
        <YAxis
          type="number"
          dataKey={yKey}
          name={series[0].label}
          tick={{ fontSize: 11 }}
          width={48}
          tickFormatter={shortNumber}
        />
        <Tooltip cursor={{ strokeDasharray: "3 3" }} />
        <Scatter name={series[0].label} dataKey={yKey} fill={COLORS[0]} />
      </ScatterChart>
    );
  } else if (chart.type === "pie") {
    const key = series[0].key;
    body = (
      <PieChart>
        <Pie
          data={data}
          dataKey={key}
          nameKey="x"
          outerRadius="68%"
          label={false}
          paddingAngle={1}
        >
          {data.map((_, index) => (
            <Cell key={index} fill={COLORS[index % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip />
        <Legend
          verticalAlign="bottom"
          height={28}
          wrapperStyle={{ fontSize: 12 }}
        />
      </PieChart>
    );
  } else if (chart.type === "line") {
    body = (
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        {axes}
        {series.map((item, index) => (
          <Line
            key={item.key}
            type="monotone"
            dataKey={item.key}
            name={item.label}
            stroke={COLORS[index % COLORS.length]}
            strokeWidth={2}
            dot={false}
          />
        ))}
      </LineChart>
    );
  } else if (chart.type === "area") {
    body = (
      <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        {axes}
        {series.map((item, index) => (
          <Area
            key={item.key}
            type="monotone"
            dataKey={item.key}
            name={item.label}
            stroke={COLORS[index % COLORS.length]}
            fill={COLORS[index % COLORS.length]}
            fillOpacity={0.2}
          />
        ))}
      </AreaChart>
    );
  } else {
    const stacked = chart.type === "stacked_bar";
    body = (
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        {axes}
        {series.map((item, index) => (
          <Bar
            key={item.key}
            dataKey={item.key}
            name={item.label}
            fill={COLORS[index % COLORS.length]}
            radius={stacked ? undefined : [4, 4, 0, 0]}
            maxBarSize={48}
            stackId={stacked ? "stack" : undefined}
          />
        ))}
      </BarChart>
    );
  }

  return (
    <div style={{ minWidth: 0 }}>
      {chart.title?.trim() ? (
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            marginBottom: 4,
            lineHeight: 1.4,
            wordBreak: "break-word",
          }}
        >
          {chart.title}
        </div>
      ) : null}
      <div style={{ width: "100%", height: 240, minWidth: 0 }}>
        <ResponsiveContainer width="100%" height="100%">
          {body}
        </ResponsiveContainer>
      </div>
      {chart.sampled ? (
        <div style={{ fontSize: 12, opacity: 0.65, marginTop: 2 }}>
          {t("apps.assistantChartSampled", {
            count: chart.sample_size ?? 0,
            defaultValue: "Based on the first {{count}} rows",
          })}
        </div>
      ) : null}
    </div>
  );
}
