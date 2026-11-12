import { request } from "../request";

/** One MiniApp placed on a board's 12-column grid. */
export interface BoardItem {
  app_id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  /** Keep this cell in place during auto-arrange; disables drag and resize. */
  locked?: boolean;
}

export interface BoardLayout {
  version: number;
  items: BoardItem[];
}

export interface BoardSummary {
  board_id: string;
  name: string;
  description: string;
  /** CAS version; send it back when saving a layout. */
  version: number;
  item_count: number;
  created_at: number;
  updated_at: number;
}

export interface BoardDetail extends BoardSummary {
  layout: BoardLayout;
}

export interface CreateBoardInput {
  name: string;
  description?: string;
}

export interface LayoutInput {
  version: number;
  items: BoardItem[];
}

function jsonBody(value: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

export const boardsApi = {
  list: () => request<BoardSummary[]>("/boards"),

  create: (input: CreateBoardInput) =>
    request<BoardDetail>("/boards", jsonBody(input)),

  get: (boardId: string) =>
    request<BoardDetail>(`/boards/${encodeURIComponent(boardId)}`),

  update: (boardId: string, input: Partial<CreateBoardInput>) =>
    request<BoardDetail>(`/boards/${encodeURIComponent(boardId)}`, {
      ...jsonBody(input),
      method: "PATCH",
    }),

  remove: (boardId: string) =>
    request<{ deleted: string }>(`/boards/${encodeURIComponent(boardId)}`, {
      method: "DELETE",
    }),

  setLayout: (boardId: string, input: LayoutInput) =>
    request<BoardDetail>(`/boards/${encodeURIComponent(boardId)}/layout`, {
      ...jsonBody(input),
      method: "PUT",
    }),
};
