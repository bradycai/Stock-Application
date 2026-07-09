import { EMPTY_FILTERS } from "../lib/filters";

export default function Filters({ value, onChange, actions = ["buy", "sell", "watch", "avoid"] }) {
  const set = (patch) => onChange({ ...value, ...patch });
  return (
    <div className="filters">
      <input
        className="filter-input"
        placeholder="Ticker"
        value={value.ticker}
        onChange={(e) => set({ ticker: e.target.value })}
      />
      <select
        className="filter-input"
        value={value.action}
        onChange={(e) => set({ action: e.target.value })}
      >
        <option value="">All actions</option>
        {actions.map((a) => (
          <option key={a} value={a}>
            {a.toUpperCase()}
          </option>
        ))}
      </select>
      <label className="filter-conf">
        min conf
        <input
          type="number"
          min="0"
          max="100"
          className="filter-input filter-num"
          value={value.minConfidence}
          onChange={(e) => set({ minConfidence: e.target.value })}
        />
        %
      </label>
      <input
        type="date"
        className="filter-input"
        value={value.since}
        onChange={(e) => set({ since: e.target.value })}
      />
      <button className="btn btn-small" onClick={() => onChange({ ...EMPTY_FILTERS })}>
        Reset
      </button>
    </div>
  );
}
