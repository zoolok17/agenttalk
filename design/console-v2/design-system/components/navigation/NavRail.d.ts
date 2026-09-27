import * as React from 'react';

export interface NavRailItem {
  label: string;
  /** SVG path-`d` string (Lucide-style) or a ready SVG node. */
  icon: string | React.ReactNode;
  active?: boolean;
  /** Count badge (e.g. open attention items). Falsy = hidden. */
  badge?: number;
  onClick?: () => void;
}
export interface NavRailLegendRow {
  label: string;
  /** CSS color, e.g. 'var(--ok)'. */
  color: string;
  count: number;
}
export interface NavRailProps {
  items: NavRailItem[];
  legend?: NavRailLegendRow[];
  width?: number;
  style?: React.CSSProperties;
}

/**
 * Left sidebar: view list with active state + count badges, and a status legend.
 * @startingPoint section="Navigation" subtitle="Console sidebar nav + status legend" viewport="238x520"
 */
export function NavRail(props: NavRailProps): JSX.Element;
