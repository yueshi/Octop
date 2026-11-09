import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AppAssistantChart } from "../../api/modules/apps";
import AssistantChart from "./AssistantChart";

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => (
      <div style={{ width: 600, height: 240 }}>{children}</div>
    ),
  };
});

const chart: AppAssistantChart = {
  type: "bar",
  title: "Sales by status",
  x: "status",
  series: [{ key: "qty", label: "Qty" }],
  data: [
    { x: "new", qty: 5 },
    { x: "done", qty: 1 },
  ],
  sampled: true,
  sample_size: 500,
};

describe("AssistantChart", () => {
  it("renders the title and sample note", () => {
    render(<AssistantChart chart={chart} />);
    expect(screen.getByText("Sales by status")).toBeInTheDocument();
    expect(screen.getByText(/Based on the first 500 rows/)).toBeInTheDocument();
  });

  it("renders an empty state without data", () => {
    render(<AssistantChart chart={{ ...chart, data: [], series: [] }} />);
    expect(screen.getByText("No chartable data")).toBeInTheDocument();
  });

  it("renders scatter charts", () => {
    render(
      <AssistantChart
        chart={{
          ...chart,
          type: "scatter",
          title: "Qty vs Amount",
          series: [{ key: "qty", label: "Qty" }],
          data: [
            { x: 1, qty: 2 },
            { x: 2, qty: 4 },
          ],
        }}
      />,
    );
    expect(screen.getByText("Qty vs Amount")).toBeInTheDocument();
  });

  it("renders stacked bar charts", () => {
    render(
      <AssistantChart
        chart={{
          ...chart,
          type: "stacked_bar",
          title: "Composition",
          series: [
            { key: "qty", label: "Qty" },
            { key: "amount", label: "Amount" },
          ],
          data: [
            { x: "new", qty: 5, amount: 15 },
            { x: "done", qty: 1, amount: 7 },
          ],
        }}
      />,
    );
    expect(screen.getByText("Composition")).toBeInTheDocument();
  });
});
