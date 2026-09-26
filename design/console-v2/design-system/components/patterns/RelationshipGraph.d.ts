import * as React from 'react';
import type { AgentState } from '../core/StatusDot';

export interface GraphNode {
  id: string;
  state?: AgentState;
  /** Optional explicit position; omit to auto-layout on a circle. */
  x?: number;
  y?: number;
}
export interface GraphEdge {
  from: string;
  to: string;
  /** Message volume — scales stroke width. Default 1. */
  weight?: number;
  /** Active review — drawn dashed + animated in accent. */
  hot?: boolean;
}
export interface RelationshipGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  width?: number;
  height?: number;
  radius?: number;
  onNodeClick?: (id: string) => void;
  style?: React.CSSProperties;
}

/**
 * Who-talks-to-whom graph: agents on a circle, edges weighted by volume,
 * active-review edge dashed + animated.
 * @startingPoint section="Patterns" subtitle="Agent message-flow graph" viewport="640x480"
 */
export function RelationshipGraph(props: RelationshipGraphProps): JSX.Element;
