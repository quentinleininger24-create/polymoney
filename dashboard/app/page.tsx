"use client";

import useSWR from "swr";

const fetcher = (url: string) => fetch(url).then((r) => r.json());

type Status = {
  mode: string;
  cash_usdc: number;
  open_positions: number;
  realized_pnl: number;
  unrealized_pnl: number;
  bankroll: number;
};

type Position = {
  market_id: string;
  outcome: string;
  strategy: string;
  entry_price: number;
  cost_basis_usdc: number;
  current_price: number | null;
  unrealized_pnl: number;
  reasoning: string | null;
};

export default function Home() {
  const { data: status } = useSWR<Status>("/api/status", fetcher, { refreshInterval: 5000 });
  const { data: positions } = useSWR<Position[]>("/api/positions", fetcher, { refreshInterval: 5000 });

  return (
    <main className="space-y-6">
      <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card label="Mode" value={status?.mode ?? "-"} />
        <Card label="Cash" value={status ? `$${status.cash_usdc.toFixed(2)}` : "-"} />
        <Card label="Open" value={status?.open_positions?.toString() ?? "-"} />
        <Card
          label="Realized PnL"
          value={status ? `$${status.realized_pnl.toFixed(2)}` : "-"}
          accent={status ? (status.realized_pnl >= 0 ? "pos" : "neg") : undefined}
        />
      </section>

      <section>
        <h2 className="mb-3 text-sm uppercase tracking-wider text-white/50">Open positions</h2>
        <div className="overflow-hidden rounded-lg border border-white/10">
          <table className="w-full text-sm">
            <thead className="bg-white/5 text-left text-xs uppercase tracking-wider text-white/50">
              <tr>
                <th className="p-3">Market</th>
                <th className="p-3">Side</th>
                <th className="p-3">Strat</th>
                <th className="p-3 text-right">Entry</th>
                <th className="p-3 text-right">Cost</th>
                <th className="p-3 text-right">PnL</th>
              </tr>
            </thead>
            <tbody>
              {(positions ?? []).map((p) => (
                <tr key={`${p.market_id}-${p.outcome}-${p.strategy}`} className="border-t border-white/5">
                  <td className="p-3 font-mono text-xs">{p.market_id.slice(0, 12)}</td>
                  <td className="p-3">{p.outcome}</td>
                  <td className="p-3 text-white/70">{p.strategy}</td>
                  <td className="p-3 text-right">{p.entry_price.toFixed(3)}</td>
                  <td className="p-3 text-right">${p.cost_basis_usdc.toFixed(2)}</td>
                  <td
                    className={`p-3 text-right ${p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}
                  >
                    {p.unrealized_pnl >= 0 ? "+" : ""}
                    ${p.unrealized_pnl.toFixed(2)}
                  </td>
                </tr>
              ))}
              {(!positions || positions.length === 0) && (
                <tr>
                  <td className="p-6 text-center text-white/40" colSpan={6}>
                    No open positions.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

function Card({ label, value, accent }: { label: string; value: string; accent?: "pos" | "neg" }) {
  const color =
    accent === "pos" ? "text-emerald-400" : accent === "neg" ? "text-red-400" : "text-white";
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div className="text-xs uppercase tracking-wider text-white/50">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}
