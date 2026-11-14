import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Tooltip, Typography } from "antd";
import type { LucideIcon } from "lucide-react";
import {
  AppWindow,
  Banknote,
  BarChart3,
  BookOpen,
  Bookmark,
  Boxes,
  Briefcase,
  CalendarDays,
  Camera,
  Check,
  CheckSquare,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Code,
  Coffee,
  Contact,
  Database,
  FileText,
  FolderKanban,
  Gamepad2,
  Gauge,
  Globe,
  GraduationCap,
  HeartPulse,
  House,
  Image,
  Lightbulb,
  LayoutGrid,
  ListTodo,
  MapPin,
  Megaphone,
  Music,
  Package,
  PieChart,
  Plane,
  Receipt,
  Rocket,
  Search,
  ShoppingCart,
  Sparkles,
  StickyNote,
  Store,
  Table2,
  Target,
  TrendingUp,
  Truck,
  Users,
  Utensils,
  Video,
  Wallet,
  Wrench,
  Award,
  Bell,
  Car,
  Crown,
  Flame,
  Gem,
  Gift,
  Headphones,
  Hotel,
  Leaf,
  Mail,
  Maximize2,
  MessageSquare,
  Minimize2,
  Mountain,
  Moon,
  Palette,
  PawPrint,
  Phone,
  QrCode,
  Server,
  ShieldCheck,
  Smartphone,
  Star,
  Sun,
  Ticket,
  Zap,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AppSummary } from "../../api/modules/apps";
import MiniAppFrame from "../../components/MiniAppFrame";
import { selectEnabledExperts, useAgent } from "../../context/AgentContext";
import { ExpertIcon } from "../Experts/components/iconForName";
import DesktopActivityCenter, {
  type DesktopActivityExpert,
  type DesktopActivityWindow,
} from "./DesktopActivityCenter";
import DesktopBootScreen from "./DesktopBootScreen";
import DesktopChatWindow from "./DesktopChatWindow";
import DesktopCommandPalette, {
  type DesktopCommandItem,
} from "./DesktopCommandPalette";
import DesktopDock, { type DesktopDockItem } from "./DesktopDock";
import DesktopMissionControl, {
  type DesktopMissionSpace,
} from "./DesktopMissionControl";
import DesktopNotificationCenter from "./DesktopNotificationCenter";
import DesktopWebView from "./DesktopWebView";
import { useDesktopNotifications } from "./desktopNotifications";
import {
  APP_ACTIONS,
  actionPromptFor,
  fillTemplate,
  type AppActionKey,
} from "./appActions";
import {
  appWindowId,
  chatWindowId,
  createDesktopWindow,
  desktopWindowTitle,
  DOCK_SPACE,
  externalWindowId,
  loadDesktopWindows,
  MENU_BAR_HEIGHT,
  saveDesktopWindows,
  WINDOW_REGISTRY,
  windowSpaceId,
  type DesktopWindowState,
} from "./desktopWindows";
import styles from "./index.module.less";

const { Text } = Typography;

const PALETTES: ReadonlyArray<readonly [string, string]> = [
  ["#5b8cff", "#7c5cff"],
  ["#ff7a59", "#ff4d8d"],
  ["#22c55e", "#0ea5e9"],
  ["#f59e0b", "#ef4444"],
  ["#8b5cf6", "#ec4899"],
  ["#06b6d4", "#3b82f6"],
  ["#f97316", "#facc15"],
  ["#14b8a6", "#84cc16"],
  ["#6366f1", "#0ea5e9"],
  ["#ec4899", "#f43f5e"],
  ["#ff9a3c", "#ff3cac"],
  ["#00c6ff", "#0072ff"],
  ["#7f00ff", "#e100ff"],
  ["#00f5a0", "#00d9f5"],
  ["#f83600", "#f9d423"],
  ["#fc466b", "#3f5efb"],
  ["#11998e", "#38ef7d"],
  ["#ff416c", "#ff4b2b"],
  ["#ffd200", "#f7971e"],
  ["#1fa2ff", "#12d8fa"],
  ["#a18cd1", "#fbc2eb"],
  ["#f093fb", "#f5576c"],
  ["#4facfe", "#00f2fe"],
  ["#43e97b", "#38f9d7"],
];

type AppIconStyle = "aurora" | "glass" | "ring" | "mesh" | "solid";
const ICON_STYLES: readonly AppIconStyle[] = [
  "aurora",
  "glass",
  "ring",
  "mesh",
  "solid",
];

const GLYPHS: ReadonlyArray<{ match: RegExp; icon: LucideIcon }> = [
  { match: /审批|流程|workflow|approval|process/i, icon: ClipboardList },
  { match: /咖啡|茶饮|coffee|cafe|tea/i, icon: Coffee },
  { match: /图片|相册|image|photo|gallery/i, icon: Image },
  { match: /占比|分布|比例|pie|breakdown/i, icon: PieChart },
  { match: /便签|笔记|note|memo/i, icon: StickyNote },
  { match: /礼物|福利|gift|reward/i, icon: Gift },
  { match: /宠物|猫|狗|pet|paw/i, icon: PawPrint },
  { match: /植物|花园|绿植|leaf|plant|garden/i, icon: Leaf },
  { match: /户外|登山|山|outdoor|mountain|hike/i, icon: Mountain },
  { match: /天气|晴|阳光|weather|sunny/i, icon: Sun },
  { match: /睡眠|夜间|月亮|sleep|night|moon/i, icon: Moon },
  { match: /评分|星级|star|rate|favorite/i, icon: Star },
  { match: /效率|自动化|快捷|zap|automation|fast/i, icon: Zap },
  { match: /服务器|运维|部署|server|ops|deploy/i, icon: Server },
  { match: /手机|移动端|smartphone|mobile/i, icon: Smartphone },
  { match: /耳机|音频|headphone|audio|podcast/i, icon: Headphones },
  { match: /设计|配色|主题|design|palette|theme/i, icon: Palette },
  { match: /汽车|车辆|car|vehicle|drive/i, icon: Car },
  { match: /酒店|住宿|民宿|hotel|stay/i, icon: Hotel },
  { match: /票务|门票|电影票|ticket/i, icon: Ticket },
  { match: /二维码|扫码|qr/i, icon: QrCode },
  { match: /安全|权限|防护|security|shield|permission/i, icon: ShieldCheck },
  { match: /提醒|通知|bell|notify|reminder/i, icon: Bell },
  { match: /邮件|邮箱|mail|email/i, icon: Mail },
  { match: /电话|phone|call/i, icon: Phone },
  { match: /奖励|成就|奖杯|award|achievement|trophy/i, icon: Award },
  { match: /会员|vip|皇冠|crown|premium/i, icon: Crown },
  { match: /宝石|珠宝|gem|jewel/i, icon: Gem },
  { match: /热门|火焰|flame|hot|trending/i, icon: Flame },
  {
    match: /台账|表格|库存|table|sheet|inventory/i,
    icon: Table2,
  },
  {
    match: /业绩|报表|统计|分析|增长|看板|chart|report|dashboard|analytics/i,
    icon: BarChart3,
  },
  {
    match: /日历|日程|排期|预约|calendar|schedule|booking/i,
    icon: CalendarDays,
  },
  { match: /阅读|书|笔记|知识|book|read|note|wiki/i, icon: BookOpen },
  {
    match: /客户|用户|成员|联系人|招聘|customer|user|member|crm|contact/i,
    icon: Users,
  },
  { match: /任务|待办|进度|打卡|task|todo|checklist/i, icon: CheckSquare },
  { match: /项目|看板|project|kanban/i, icon: FolderKanban },
  {
    match: /财务|账|预算|报销|工资|finance|budget|expense|invoice/i,
    icon: Wallet,
  },
  {
    match: /购物|订单|销售|电商|商城|order|shop|sales|commerce/i,
    icon: ShoppingCart,
  },
  { match: /健康|健身|运动|体重|health|fitness|workout/i, icon: HeartPulse },
  { match: /饮食|菜谱|餐厅|咖啡|food|recipe|coffee/i, icon: Utensils },
  { match: /旅行|行程|出差|travel|trip|flight/i, icon: Plane },
  { match: /房屋|家居|租房|home|house|rent/i, icon: House },
  { match: /职场|职业|job|career|office/i, icon: Briefcase },
  { match: /学习|课程|教育|培训|course|learn|study/i, icon: GraduationCap },
  { match: /游戏|game|gaming/i, icon: Gamepad2 },
  { match: /音乐|歌|music|song|playlist/i, icon: Music },
  { match: /照片|图片|相册|摄影|photo|image|gallery/i, icon: Camera },
  { match: /物流|发货|快递|运输|shipping|logistics|delivery/i, icon: Truck },
  { match: /门店|店铺|store|shopfront/i, icon: Store },
  { match: /营销|推广|广告|marketing|promo|ads/i, icon: Megaphone },
  { match: /视频|video|film|movie/i, icon: Video },
  { match: /代码|开发|工程|code|dev|engineering/i, icon: Code },
  { match: /数据库|database|sql/i, icon: Database },
  { match: /网站|网页|web|site|portal/i, icon: Globe },
  { match: /目标|okr|goal|target/i, icon: Target },
  { match: /灵感|想法|idea|brainstorm/i, icon: Lightbulb },
  { match: /工具|tool|utility/i, icon: Wrench },
  { match: /收藏|书签|bookmark/i, icon: Bookmark },
  { match: /地图|位置|地点|map|location/i, icon: MapPin },
  { match: /预算|储蓄|理财|saving|budget/i, icon: Banknote },
  { match: /增长|趋势|trend|growth/i, icon: TrendingUp },
  { match: /库存|物料|boxes|stock/i, icon: Boxes },
  { match: /报表|收据|账单|receipt|bill/i, icon: Receipt },
  { match: /火箭|发布|上线|launch|rocket/i, icon: Rocket },
  { match: /仪表|监控|gauge|monitor/i, icon: Gauge },
  { match: /清单|列表|list|todo/i, icon: ListTodo },
  { match: /文档|文件|document|file|doc/i, icon: FileText },
  { match: /智能|助手|assistant|sparkle|ai/i, icon: Sparkles },
  { match: /包裹|package|parcel/i, icon: Package },
  { match: /联系|contact|address/i, icon: Contact },
];

/** Apps without a keyword still get a varied glyph instead of one fallback. */
const FALLBACK_GLYPHS: readonly LucideIcon[] = [
  Sparkles,
  Rocket,
  Star,
  Zap,
  Target,
  Boxes,
  Lightbulb,
  Wrench,
  Bookmark,
  Gauge,
  Palette,
  Gem,
  Crown,
  Flame,
  Leaf,
  Sun,
  Moon,
  Award,
  Ticket,
  QrCode,
  Server,
  Smartphone,
  Music,
  Camera,
];

function hashText(text: string): number {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

export interface AppIconSpec {
  from: string;
  to: string;
  angle: number;
  style: AppIconStyle;
  Glyph: LucideIcon;
  monogram?: string;
}

/** Stable, per-app icon: palette, material and glyph derive from id + text. */
export function iconSpecForApp(app: AppSummary): AppIconSpec {
  const text = `${app.name} ${app.description}`;
  const matched = GLYPHS.find(({ match }) => match.test(text));
  const hash = hashText(app.app_id || app.name);
  const [from, to] = PALETTES[hash % PALETTES.length];
  return {
    from,
    to,
    angle: 120 + (hash % 80),
    style: ICON_STYLES[(hash >>> 8) % ICON_STYLES.length],
    Glyph:
      matched?.icon ??
      FALLBACK_GLYPHS[(hash >>> 4) % FALLBACK_GLYPHS.length] ??
      AppWindow,
    // Every app keeps a small initial so same-category apps still differ.
    monogram: Array.from(app.name.trim())[0]?.toUpperCase() ?? "?",
  };
}

const ICON_STYLE_CLASS: Record<AppIconStyle, string> = {
  aurora: styles.desktopIconArtAurora,
  glass: styles.desktopIconArtGlass,
  ring: styles.desktopIconArtRing,
  mesh: styles.desktopIconArtMesh,
  solid: styles.desktopIconArtSolid,
};

const UNASSIGNED_SPACE = "__unassigned__";
const ALL_APPS_SPACE = "__all_apps__";
const ACTIVE_SPACE_KEY = "octop:apps-desktop-space:v1";
const EMPTY_APPS: AppSummary[] = [];

interface DesktopSpace {
  id: string;
  agentId: string | null;
  kind: "expert" | "all";
  name: string;
  accent: string;
  iconName?: string | null;
  iconUrl?: string | null;
  state?: string;
  unreadCount: number;
  apps: AppSummary[];
}

function spaceIdForApp(app: AppSummary): string {
  return app.agent_id?.trim() || UNASSIGNED_SPACE;
}

interface WindowDrag {
  id: string;
  mode: "move" | "resize";
  startX: number;
  startY: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

function AppIcon({
  app,
  size,
  badge = false,
}: {
  app: AppSummary;
  size: number;
  badge?: boolean;
}) {
  const spec = iconSpecForApp(app);
  const Glyph = spec.Glyph;
  return (
    <span
      className={[styles.desktopIconArt, ICON_STYLE_CLASS[spec.style]].join(
        " ",
      )}
      style={
        {
          width: size,
          height: size,
          borderRadius: Math.round(size * 0.24),
          "--icon-from": spec.from,
          "--icon-to": spec.to,
          "--icon-angle": `${spec.angle}deg`,
        } as CSSProperties
      }
    >
      <Glyph
        size={Math.round(size * 0.44)}
        strokeWidth={1.9}
        className={styles.desktopIconGlyph}
      />
      {spec.monogram && (
        <span className={styles.desktopIconMonogram}>{spec.monogram}</span>
      )}
      {badge && app.row_count > 0 && (
        <span className={styles.desktopIconBadge}>
          {app.row_count > 99 ? "99+" : app.row_count}
        </span>
      )}
    </span>
  );
}

function DesktopIcon({
  app,
  selected,
  onSelect,
  onOpen,
}: {
  app: AppSummary;
  selected: boolean;
  onSelect: () => void;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  const label = t("apps.desktop.open", "Open");

  return (
    <Tooltip
      title={app.description ? `${app.name} — ${app.description}` : app.name}
      placement="right"
    >
      <button
        type="button"
        className={[
          styles.desktopIcon,
          selected ? styles.desktopIconSelected : "",
        ]
          .filter(Boolean)
          .join(" ")}
        data-testid="desktop-icon"
        data-app-id={app.app_id}
        aria-label={`${label} ${app.name}`}
        aria-pressed={selected}
        onClick={onSelect}
        onContextMenu={(event) => {
          event.preventDefault();
          onSelect();
        }}
        onDoubleClick={onOpen}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onOpen();
          }
        }}
      >
        <AppIcon app={app} size={72} badge />
        <Text ellipsis className={styles.desktopIconName}>
          {app.name}
        </Text>
      </button>
    </Tooltip>
  );
}

/**
 * macOS-style desktop: generated squircle icons on a dark wallpaper, a glass
 * dock, and draggable/resizable app windows with traffic-light controls.
 */
export default function DesktopGrid({
  apps,
  autoFullscreen = false,
}: {
  apps: AppSummary[];
  /** Set only when the user explicitly switches to desktop mode. */
  autoFullscreen?: boolean;
}) {
  const { t } = useTranslation();
  const { agents } = useAgent();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<WindowDrag | null>(null);
  const zRef = useRef(20);
  const seedRef = useRef(0);
  const fullscreenRequestedRef = useRef(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [booting, setBooting] = useState(Boolean(autoFullscreen));
  const [windows, setWindows] =
    useState<DesktopWindowState[]>(loadDesktopWindows);
  const [spaceId, setSpaceId] = useState(
    () => localStorage.getItem(ACTIVE_SPACE_KEY) ?? "",
  );
  const [selectedAppId, setSelectedAppId] = useState("");
  const [spacesOpen, setSpacesOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [missionOpen, setMissionOpen] = useState(false);
  const notifications = useDesktopNotifications();
  const toggleCommandPalette = useCallback(() => {
    setSpacesOpen(false);
    setMissionOpen(false);
    setCommandOpen((value) => !value);
  }, []);
  const [visibleTitleBars, setVisibleTitleBars] = useState<Set<string>>(
    new Set(),
  );
  const titleBarTimersRef = useRef(new Map<string, number>());
  const windowsRef = useRef(windows);
  useEffect(() => {
    windowsRef.current = windows;
  }, [windows]);

  useEffect(() => {
    const timer = window.setTimeout(() => saveDesktopWindows(windows), 250);
    return () => window.clearTimeout(timer);
  }, [windows]);

  useEffect(() => {
    const onUnload = () => saveDesktopWindows(windowsRef.current);
    window.addEventListener("beforeunload", onUnload);
    return () => {
      window.removeEventListener("beforeunload", onUnload);
      saveDesktopWindows(windowsRef.current);
    };
  }, []);

  useEffect(() => {
    zRef.current = Math.max(zRef.current, ...windows.map((win) => win.z));
  }, [windows]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const maxW = root.clientWidth;
    const maxH = root.clientHeight;
    setWindows((list) => {
      let changed = false;
      const next = list.map((win) => {
        if (win.maximized) return win;
        const w = Math.min(win.w, Math.max(220, maxW - 8));
        const h = Math.min(win.h, Math.max(180, maxH - MENU_BAR_HEIGHT - 8));
        const x = Math.max(0, Math.min(Math.max(0, maxW - w), win.x));
        const y = Math.max(
          MENU_BAR_HEIGHT,
          Math.min(Math.max(MENU_BAR_HEIGHT, maxH - 40), win.y),
        );
        if (x === win.x && y === win.y && w === win.w && h === win.h) {
          return win;
        }
        changed = true;
        return { ...win, x, y, w, h };
      });
      return changed ? next : list;
    });
  }, []);

  const byId = useMemo(
    () => new Map(apps.map((app) => [app.app_id, app])),
    [apps],
  );
  const ordered = useMemo(
    () =>
      [...apps].sort((left, right) =>
        left.name.localeCompare(right.name, undefined, {
          numeric: true,
          sensitivity: "base",
        }),
      ),
    [apps],
  );
  const spaces = useMemo(() => {
    const agentById = new Map(agents.map((agent) => [agent.agent_id, agent]));
    const groups = new Map<string, AppSummary[]>();
    for (const app of ordered) {
      const id = spaceIdForApp(app);
      const group = groups.get(id);
      if (group) group.push(app);
      else groups.set(id, [app]);
    }
    // Every enabled expert gets a persistent workspace even before its first
    // MiniApp exists; empty desktops offer a direct route into expert chat.
    for (const agent of selectEnabledExperts(agents, null)) {
      if (!groups.has(agent.agent_id)) groups.set(agent.agent_id, []);
    }
    const result: DesktopSpace[] = Array.from(groups, ([id, group]) => {
      const agent = agentById.get(id);
      const fallback = PALETTES[hashText(id) % PALETTES.length];
      return {
        id,
        agentId: id === UNASSIGNED_SPACE ? null : id,
        kind: "expert" as const,
        name:
          id === UNASSIGNED_SPACE
            ? t("apps.desktop.unassigned", "Unassigned MiniApps")
            : agent?.name || group[0]?.agent_name || id,
        accent: agent?.color || fallback[0],
        iconName: agent?.icon_name ?? null,
        iconUrl: agent?.icon_url ?? null,
        state: agent?.state,
        unreadCount: agent?.unread_count ?? 0,
        apps: group,
      };
    });
    result.sort((left, right) => {
      if (left.id === UNASSIGNED_SPACE) return 1;
      if (right.id === UNASSIGNED_SPACE) return -1;
      return left.name.localeCompare(right.name, undefined, {
        numeric: true,
        sensitivity: "base",
      });
    });
    result.push({
      id: ALL_APPS_SPACE,
      agentId: null,
      kind: "all",
      name: t("apps.desktop.allApps", "All Apps"),
      accent: "#8b5cf6",
      iconName: "layout-grid",
      iconUrl: null,
      state: "running",
      unreadCount: 0,
      apps: ordered,
    });
    return result;
  }, [agents, ordered, t]);
  const space = spaces.find((item) => item.id === spaceId) ?? spaces[0] ?? null;
  const currentApps = space?.apps ?? EMPTY_APPS;
  const selectedApp =
    currentApps.find((app) => app.app_id === selectedAppId) ?? null;
  const appSpace = useMemo(
    () =>
      new Map(ordered.map((app) => [app.app_id, spaceIdForApp(app)] as const)),
    [ordered],
  );
  const spaceNameById = useMemo(
    () =>
      new Map(spaces.map((item) => [item.id, { name: item.name }] as const)),
    [spaces],
  );
  const windowTitles = useMemo(
    () =>
      new Map(
        windows.map(
          (win) =>
            [
              win.id,
              desktopWindowTitle(win, { apps: byId, spaces: spaceNameById }),
            ] as const,
        ),
      ),
    [byId, spaceNameById, windows],
  );

  useEffect(() => {
    if (!space || space.id === spaceId) return;
    setSpaceId(space.id);
  }, [space, spaceId]);

  useEffect(() => {
    if (space?.id) localStorage.setItem(ACTIVE_SPACE_KEY, space.id);
  }, [space?.id]);

  useEffect(() => {
    setSelectedAppId((current) =>
      current && currentApps.some((app) => app.app_id === current)
        ? current
        : "",
    );
  }, [currentApps]);

  const selectSpace = useCallback((nextId: string) => {
    setSpaceId(nextId);
    setSelectedAppId("");
    setSpacesOpen(false);
  }, []);

  const moveSpace = useCallback(
    (delta: number) => {
      if (!space || spaces.length < 2) return;
      const index = spaces.findIndex((item) => item.id === space.id);
      const next = spaces[(index + delta + spaces.length) % spaces.length];
      if (next) selectSpace(next.id);
    },
    [selectSpace, space, spaces],
  );

  const tr = useCallback(
    (key: string, fallback: string) => t(key, fallback),
    [t],
  );

  const openChatWindow = useCallback(
    (
      agentId: string,
      options?: { app?: AppSummary | null; prompt?: string },
    ) => {
      const id = chatWindowId(agentId);
      const nextSeed = options?.prompt
        ? {
            id: ++seedRef.current,
            text: options.prompt,
            appId: options.app?.app_id,
          }
        : undefined;
      setWindows((list) => {
        const existing = list.find((win) => win.id === id);
        if (existing) {
          const z = ++zRef.current;
          return list.map((win) =>
            win.id === id
              ? {
                  ...win,
                  minimized: false,
                  z,
                  seed: nextSeed ?? win.seed,
                  contextAppId: options?.app?.app_id ?? win.contextAppId,
                }
              : win,
          );
        }
        const root = rootRef.current;
        const win = createDesktopWindow({
          kind: "chat",
          agentId,
          spaceId: agentId,
          contextAppId: options?.app?.app_id ?? null,
          seed: nextSeed,
          z: ++zRef.current,
          list,
          appSpace,
          viewportWidth: root?.clientWidth ?? 1280,
          viewportHeight: root?.clientHeight ?? 800,
        });
        return win ? [...list, win] : list;
      });
    },
    [appSpace],
  );

  const openExternalWindow = useCallback(
    (url: string, spaceId: string) => {
      const target = url.trim();
      const id = externalWindowId(spaceId, target);
      if (!id) return;
      setWindows((list) => {
        const existing = list.find((win) => win.id === id);
        if (existing) {
          const z = ++zRef.current;
          return list.map((win) =>
            win.id === id ? { ...win, minimized: false, z } : win,
          );
        }
        const root = rootRef.current;
        const win = createDesktopWindow({
          kind: "external",
          spaceId,
          url: target,
          z: ++zRef.current,
          list,
          appSpace,
          viewportWidth: root?.clientWidth ?? 1280,
          viewportHeight: root?.clientHeight ?? 800,
        });
        return win ? [...list, win] : list;
      });
    },
    [appSpace],
  );

  const consumeChatSeed = useCallback((id: string) => {
    setWindows((list) =>
      list.map((win) =>
        win.id === id && win.seed ? { ...win, seed: undefined } : win,
      ),
    );
  }, []);

  const openNotificationWindow = useCallback(
    (id: string) => {
      const win = windows.find((item) => item.id === id);
      if (!win) return;
      const targetSpace = windowSpaceId(win, appSpace);
      if (targetSpace && targetSpace !== spaceId) {
        selectSpace(targetSpace);
      }
      setWindows((list) =>
        list.map((item) =>
          item.id === id
            ? { ...item, minimized: false, z: ++zRef.current }
            : item,
        ),
      );
    },
    [appSpace, selectSpace, spaceId, windows],
  );

  const clearTitleBarTimer = useCallback((id: string) => {
    const timer = titleBarTimersRef.current.get(id);
    if (timer === undefined) return;
    window.clearTimeout(timer);
    titleBarTimersRef.current.delete(id);
  }, []);

  const revealTitleBar = useCallback(
    (id: string) => {
      clearTitleBarTimer(id);
      setVisibleTitleBars((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
    },
    [clearTitleBarTimer],
  );

  const scheduleHideTitleBar = useCallback(
    (id: string) => {
      clearTitleBarTimer(id);
      const timer = window.setTimeout(() => {
        titleBarTimersRef.current.delete(id);
        setVisibleTitleBars((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }, 220);
      titleBarTimersRef.current.set(id, timer);
    },
    [clearTitleBarTimer],
  );

  useEffect(() => {
    const liveIds = new Set(windows.map((win) => win.id));
    setVisibleTitleBars((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const id of prev) {
        if (!liveIds.has(id)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [windows]);

  useEffect(
    () => () => {
      for (const timer of titleBarTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      titleBarTimersRef.current.clear();
    },
    [],
  );

  const runAction = useCallback(
    (key: AppActionKey, app: AppSummary, agentId: string) => {
      const action = APP_ACTIONS.find((item) => item.key === key);
      if (!action) return;
      openChatWindow(agentId, {
        app,
        prompt: actionPromptFor(action, app, tr),
      });
    },
    [openChatWindow, tr],
  );

  const openPetChat = useCallback(() => {
    if (!space?.agentId) return;
    openChatWindow(space.agentId, { app: selectedApp ?? undefined });
  }, [openChatWindow, selectedApp, space?.agentId]);

  useEffect(() => {
    setWindows((list) =>
      list.filter(
        (win) =>
          win.kind === "chat" || (Boolean(win.appId) && byId.has(win.appId!)),
      ),
    );
  }, [byId]);

  useEffect(() => {
    const root = rootRef.current;
    const sync = () => setFullscreen(document.fullscreenElement === root);
    document.addEventListener("fullscreenchange", sync);
    return () => {
      document.removeEventListener("fullscreenchange", sync);
      if (document.fullscreenElement === root) {
        void document.exitFullscreen().catch(() => {});
      }
    };
  }, []);

  useLayoutEffect(() => {
    if (!autoFullscreen || fullscreenRequestedRef.current) return;
    fullscreenRequestedRef.current = true;
    const root = rootRef.current;
    if (!root || document.fullscreenElement) return;
    try {
      const request = root.requestFullscreen?.();
      void request?.catch(() => {});
    } catch {
      // Safari / embedded browsers can reject outside a direct gesture; the
      // boot sequence still runs and the menu fullscreen button remains.
    }
  }, [autoFullscreen]);

  useEffect(() => {
    if (autoFullscreen) setBooting(true);
  }, [autoFullscreen]);

  const finishBoot = useCallback(() => setBooting(false), []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (commandOpen) {
        setCommandOpen(false);
        return;
      }
      if (missionOpen) {
        setMissionOpen(false);
        return;
      }
      setSpacesOpen(false);
      setSelectedAppId("");
      if (document.fullscreenElement === rootRef.current) {
        void document.exitFullscreen().catch(() => {});
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [commandOpen, missionOpen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (booting) return;
      if (event.key === "F3") {
        event.preventDefault();
        setCommandOpen(false);
        setSpacesOpen(false);
        setMissionOpen((value) => !value);
        return;
      }
      const mod = event.metaKey || event.ctrlKey;
      if (!mod) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        toggleCommandPalette();
        return;
      }
      if (commandOpen || booting) return;
      if (key !== "arrowleft" && key !== "arrowright") return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest("input, textarea, [contenteditable='true']")
      ) {
        return;
      }
      event.preventDefault();
      moveSpace(key === "arrowleft" ? -1 : 1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [booting, commandOpen, moveSpace, toggleCommandPalette]);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const root = rootRef.current;
      const maxW = root?.clientWidth ?? 1280;
      const maxH = root?.clientHeight ?? 800;
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      setWindows((list) =>
        list.map((win) => {
          if (win.id !== drag.id || win.maximized) return win;
          if (drag.mode === "move") {
            return {
              ...win,
              x: Math.max(0, Math.min(maxW - win.w, drag.x + dx)),
              y: Math.max(MENU_BAR_HEIGHT, Math.min(maxH - 48, drag.y + dy)),
            };
          }
          const spec = WINDOW_REGISTRY[win.kind];
          const minW = spec.minWidth;
          const minH = spec.minHeight;
          return {
            ...win,
            w: Math.max(minW, Math.min(maxW - win.x, drag.w + dx)),
            h: Math.max(minH, Math.min(maxH - win.y - 8, drag.h + dy)),
          };
        }),
      );
    };
    const onPointerUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const maxW = root.clientWidth;
      const maxH = root.clientHeight;
      setWindows((list) =>
        list.map((win) =>
          win.maximized
            ? {
                ...win,
                x: 0,
                y: MENU_BAR_HEIGHT,
                w: maxW,
                h: Math.max(
                  WINDOW_REGISTRY.app.minHeight,
                  maxH - MENU_BAR_HEIGHT - DOCK_SPACE,
                ),
              }
            : win,
        ),
      );
    });
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  const toggleFullscreen = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    if (document.fullscreenElement === root) {
      void document.exitFullscreen().catch(() => {});
    } else {
      void root.requestFullscreen().catch(() => {});
    }
  }, []);

  const focusWindow = useCallback((id: string) => {
    const z = ++zRef.current;
    setWindows((list) =>
      list.map((win) => (win.id === id ? { ...win, z } : win)),
    );
  }, []);

  const openWindow = useCallback(
    (app: AppSummary) => {
      setSelectedAppId(app.app_id);
      const targetSpace = appSpace.get(app.app_id);
      // All Apps is a library, not a window space: launching from it jumps to
      // the expert desktop that owns the MiniApp.
      if (space?.kind === "all" && targetSpace) setSpaceId(targetSpace);
      const id = appWindowId(app.app_id);
      setWindows((list) => {
        const existing = list.find((win) => win.id === id);
        if (existing) {
          const z = ++zRef.current;
          return list.map((win) =>
            win.id === id ? { ...win, minimized: false, z } : win,
          );
        }
        const root = rootRef.current;
        const win = createDesktopWindow({
          kind: "app",
          appId: app.app_id,
          spaceId: targetSpace,
          z: ++zRef.current,
          list,
          appSpace,
          viewportWidth: root?.clientWidth ?? 1280,
          viewportHeight: root?.clientHeight ?? 800,
        });
        return win ? [...list, win] : list;
      });
    },
    [appSpace, space?.kind],
  );

  const commandItems = useMemo<DesktopCommandItem[]>(() => {
    const items: DesktopCommandItem[] = [];

    for (const app of ordered) {
      items.push({
        id: `app:${app.app_id}`,
        group: "apps",
        label: app.name,
        hint: app.agent_name,
        keywords: `${app.description} ${app.agent_name}`,
        icon: <AppWindow size={16} />,
        run: () => {
          const target = appSpace.get(app.app_id);
          if (target) selectSpace(target);
          openWindow(app);
        },
      });
    }

    for (const item of spaces) {
      items.push({
        id: `expert:${item.id}`,
        group: "experts",
        label: item.name,
        hint: fillTemplate(
          tr("apps.desktop.command.appsCount", "{{count}} MiniApps"),
          { count: item.apps.length },
        ),
        icon:
          item.kind === "all" ? (
            <LayoutGrid size={16} />
          ) : (
            <ExpertIcon
              iconName={item.iconName}
              iconUrl={item.iconUrl}
              size={16}
            />
          ),
        run: () => selectSpace(item.id),
      });
    }

    for (const win of windows) {
      const winSpaceId = windowSpaceId(win, appSpace);
      const winSpace = winSpaceId
        ? spaces.find((item) => item.id === winSpaceId)
        : undefined;
      const label = windowTitles.get(win.id) ?? "";
      if (!label) continue;
      const WinIcon = WINDOW_REGISTRY[win.kind].icon;
      const spec = WINDOW_REGISTRY[win.kind];
      items.push({
        id: `window:${win.id}`,
        group: "windows",
        label,
        hint: tr(spec.labelKey, spec.labelFallback),
        keywords: `${win.url ?? ""} ${winSpace?.name ?? ""}`,
        icon: <WinIcon size={16} />,
        run: () => openNotificationWindow(win.id),
      });
    }

    if (space?.agentId) {
      items.push({
        id: "action:chat",
        group: "actions",
        label: fillTemplate(
          tr("apps.desktop.command.chatWith", "Chat with {{name}}"),
          { name: space.name },
        ),
        hint: tr(
          "apps.desktop.command.chatHint",
          "Open the expert conversation",
        ),
        icon: <MessageSquare size={16} />,
        run: () =>
          openChatWindow(space.agentId!, {
            app: selectedApp ?? undefined,
          }),
      });
    }

    items.push({
      id: "action:mission",
      group: "actions",
      label: tr("apps.desktop.mission.button", "Mission Control"),
      hint: tr(
        "apps.desktop.mission.actionHint",
        "See every desktop and open window",
      ),
      icon: <LayoutGrid size={16} />,
      run: () => setMissionOpen(true),
    });

    items.push({
      id: "action:all-apps",
      group: "actions",
      label: tr("apps.desktop.allApps", "All Apps"),
      hint: tr("apps.desktop.allAppsHint", "Open the global MiniApp library"),
      icon: <LayoutGrid size={16} />,
      run: () => selectSpace(ALL_APPS_SPACE),
    });

    items.push({
      id: "action:fullscreen",
      group: "actions",
      label: fullscreen
        ? tr("apps.desktop.exitFullscreen", "Exit full screen")
        : tr("apps.desktop.fullscreen", "Full screen"),
      icon: fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />,
      run: toggleFullscreen,
    });

    return items;
  }, [
    appSpace,
    fullscreen,
    openChatWindow,
    openNotificationWindow,
    openWindow,
    ordered,
    selectSpace,
    selectedApp,
    space,
    spaces,
    toggleFullscreen,
    tr,
    windowTitles,
    windows,
  ]);

  const closeWindow = useCallback((id: string) => {
    setWindows((list) => {
      const rest = list.filter((win) => win.id !== id);
      const top = rest
        .filter((win) => !win.minimized)
        .sort((left, right) => right.z - left.z)[0];
      if (!top) return rest;
      const z = ++zRef.current;
      return rest.map((win) => (win.id === top.id ? { ...win, z } : win));
    });
  }, []);

  const minimizeWindow = useCallback((id: string) => {
    setWindows((list) =>
      list.map((win) => (win.id === id ? { ...win, minimized: true } : win)),
    );
  }, []);

  const toggleMaximize = useCallback((id: string) => {
    setWindows((list) =>
      list.map((win) => {
        if (win.id !== id) return win;
        const root = rootRef.current;
        const maxW = root?.clientWidth ?? 1280;
        const maxH = root?.clientHeight ?? 800;
        if (win.maximized) {
          const restore = win.restore ?? {
            x: win.x,
            y: win.y,
            w: win.w,
            h: win.h,
          };
          return {
            ...win,
            ...restore,
            maximized: false,
            restore: undefined,
            z: ++zRef.current,
          };
        }
        return {
          ...win,
          restore: { x: win.x, y: win.y, w: win.w, h: win.h },
          x: 0,
          y: MENU_BAR_HEIGHT,
          w: maxW,
          h: Math.max(
            WINDOW_REGISTRY.app.minHeight,
            maxH - MENU_BAR_HEIGHT - DOCK_SPACE,
          ),
          maximized: true,
          z: ++zRef.current,
        };
      }),
    );
  }, []);

  const startDrag = useCallback(
    (win: DesktopWindowState, event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0 || win.maximized) return;
      if ((event.target as HTMLElement).closest("button")) return;
      event.preventDefault();
      dragRef.current = {
        id: win.id,
        mode: "move",
        startX: event.clientX,
        startY: event.clientY,
        x: win.x,
        y: win.y,
        w: win.w,
        h: win.h,
      };
      focusWindow(win.id);
    },
    [focusWindow],
  );

  const startResize = useCallback(
    (win: DesktopWindowState, event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0 || win.maximized) return;
      event.preventDefault();
      event.stopPropagation();
      dragRef.current = {
        id: win.id,
        mode: "resize",
        startX: event.clientX,
        startY: event.clientY,
        x: win.x,
        y: win.y,
        w: win.w,
        h: win.h,
      };
      focusWindow(win.id);
    },
    [focusWindow],
  );

  const currentWindows = useMemo(
    () => windows.filter((win) => windowSpaceId(win, appSpace) === space?.id),
    [appSpace, space?.id, windows],
  );
  const topZ = currentWindows
    .filter((win) => !win.minimized)
    .reduce((max, win) => Math.max(max, win.z), 0);
  const chatOpen = currentWindows.some(
    (win) => win.kind === "chat" && win.agentId === space?.agentId,
  );
  const spaceById = new Map(spaces.map((item) => [item.id, item] as const));
  const ownerOf = (win: DesktopWindowState): DesktopSpace | undefined => {
    const id = windowSpaceId(win, appSpace);
    return id ? spaceById.get(id) : undefined;
  };
  const expertArt = (
    expert: DesktopSpace | undefined,
    size: number,
    unread = 0,
  ) => (
    <span
      className={styles.desktopWindowExpertArt}
      style={
        {
          width: size,
          height: size,
          borderRadius: Math.round(size * 0.24),
          "--space-accent": expert?.accent ?? "#6d7cff",
        } as CSSProperties
      }
      aria-hidden
    >
      <ExpertIcon
        iconName={expert?.iconName}
        iconUrl={expert?.iconUrl}
        size={Math.round(size * 0.5)}
      />
      {unread > 0 && (
        <span className={styles.desktopDockUnread}>
          {unread > 9 ? "9+" : unread}
        </span>
      )}
    </span>
  );
  const webArt = (size: number) => (
    <span
      className={styles.desktopWindowWebArt}
      style={{
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.24),
      }}
      aria-hidden
    >
      <Globe size={Math.round(size * 0.46)} />
    </span>
  );
  const windowArt = (win: DesktopWindowState, size: number) => {
    if (win.kind === "app" && win.appId) {
      const app = byId.get(win.appId);
      return app ? (
        <AppIcon app={app} size={size} />
      ) : (
        expertArt(ownerOf(win), size)
      );
    }
    if (win.kind === "external") return webArt(size);
    return expertArt(ownerOf(win), size);
  };

  const missionSpaces: DesktopMissionSpace[] = spaces.map((item) => ({
    id: item.id,
    name: item.name,
    accent: item.accent,
    icon:
      item.kind === "all" ? (
        <LayoutGrid size={17} />
      ) : (
        <ExpertIcon iconName={item.iconName} iconUrl={item.iconUrl} size={17} />
      ),
    active: item.id === space?.id,
    windows:
      item.kind === "all"
        ? []
        : windows
            .filter((win) => windowSpaceId(win, appSpace) === item.id)
            .sort((left, right) => right.z - left.z)
            .map((win) => ({
              id: win.id,
              title: windowTitles.get(win.id) ?? "",
              kind: win.kind,
              minimized: win.minimized,
              icon: windowArt(win, 32),
            })),
  }));

  const activityExperts: DesktopActivityExpert[] = spaces
    .filter((item) => item.kind === "expert" && item.agentId)
    .map((item) => ({
      id: item.agentId!,
      name: item.name,
      state: item.state,
      accent: item.accent,
      icon: (
        <ExpertIcon iconName={item.iconName} iconUrl={item.iconUrl} size={16} />
      ),
      openWindows: windows.filter(
        (win) => windowSpaceId(win, appSpace) === item.id,
      ).length,
    }));

  const activityWindows: DesktopActivityWindow[] = windows
    .map((win) => {
      const owner = ownerOf(win);
      return {
        id: win.id,
        title: windowTitles.get(win.id) ?? "",
        kind: win.kind,
        minimized: win.minimized,
        icon: windowArt(win, 28),
        spaceId: owner?.id ?? "",
        spaceName: owner?.name ?? "",
      };
    })
    .sort((left, right) => left.spaceName.localeCompare(right.spaceName));

  const dockItems: DesktopDockItem[] = currentApps.map((app) => {
    const win = currentWindows.find(
      (item) => item.kind === "app" && item.appId === app.app_id,
    );
    return {
      id: `dock:app:${app.app_id}`,
      kind: "app",
      appId: app.app_id,
      windowId: win?.id,
      label: app.name,
      icon: <AppIcon app={app} size={46} />,
      running: Boolean(win),
      minimized: win?.minimized,
      active: Boolean(win && !win.minimized && win.z === topZ),
      onActivate: () => openWindow(app),
      onMinimize: win ? () => minimizeWindow(win.id) : undefined,
      onClose: win ? () => closeWindow(win.id) : undefined,
    };
  });
  const chatWindows = currentWindows.filter((win) => win.kind === "chat");
  for (const win of chatWindows) {
    const owner = ownerOf(win);
    dockItems.push({
      id: `dock:chat:${win.id}`,
      kind: "chat",
      windowId: win.id,
      label: windowTitles.get(win.id) ?? owner?.name ?? "",
      icon: expertArt(owner, 46, owner?.unreadCount ?? 0),
      running: true,
      minimized: win.minimized,
      active: !win.minimized && win.z === topZ,
      onActivate: () => openNotificationWindow(win.id),
      onMinimize: () => minimizeWindow(win.id),
      onClose: () => closeWindow(win.id),
    });
  }
  if (
    space?.agentId &&
    space.kind === "expert" &&
    !chatWindows.some((win) => win.agentId === space.agentId)
  ) {
    dockItems.push({
      id: `dock:chat-launch:${space.agentId}`,
      kind: "chat",
      label: fillTemplate(t("apps.desktop.chatWith", "Chat with {{name}}"), {
        name: space.name,
      }),
      icon: expertArt(space, 46, space.unreadCount),
      running: false,
      active: false,
      onActivate: () => openChatWindow(space.agentId!),
    });
  }
  for (const win of currentWindows.filter((item) => item.kind === "external")) {
    dockItems.push({
      id: `dock:external:${win.id}`,
      kind: "external",
      windowId: win.id,
      label: windowTitles.get(win.id) ?? "",
      icon: webArt(46),
      running: true,
      minimized: win.minimized,
      active: !win.minimized && win.z === topZ,
      onActivate: () => openNotificationWindow(win.id),
      onMinimize: () => minimizeWindow(win.id),
      onClose: () => closeWindow(win.id),
    });
  }
  dockItems.push({
    id: "dock:all-apps",
    kind: "launcher",
    label: t("apps.desktop.allApps", "All Apps"),
    icon: (
      <span className={styles.desktopDockLauncherArt} aria-hidden>
        <LayoutGrid size={20} />
      </span>
    ),
    active: space?.kind === "all",
    onActivate: () => selectSpace(ALL_APPS_SPACE),
  });

  const activeChatWindow = space?.agentId
    ? windows.find(
        (win) => win.kind === "chat" && win.agentId === space.agentId,
      )
    : undefined;
  const pendingApproval = notifications.some(
    (item) =>
      !item.read &&
      (item.kind === "approval" || item.kind === "security") &&
      (!item.windowId || item.windowId === activeChatWindow?.id),
  );
  const petState =
    space?.state !== "running"
      ? "offline"
      : pendingApproval
      ? "alert"
      : chatOpen
      ? "working"
      : "idle";
  const bootSpace =
    space?.kind === "expert"
      ? space
      : spaces.find((item) => item.kind === "expert") ?? null;

  return (
    <div
      ref={rootRef}
      className={styles.desktopRoot}
      data-testid="desktop-root"
      style={
        {
          "--desktop-accent": space?.accent ?? "#5865f2",
        } as CSSProperties
      }
    >
      <div className={styles.desktopWallpaper} aria-hidden>
        <span className={styles.desktopWallpaperGlow} />
      </div>
      <div className={styles.desktopMenuBar}>
        <span className={styles.desktopMenuBrand}>
          {t("apps.title", "Workbench")}
        </span>
        {spaces.length > 0 && (
          <div className={styles.desktopSpaces} data-testid="desktop-switcher">
            <button
              type="button"
              className={styles.desktopSpaceArrow}
              aria-label={t("apps.desktop.previousSpace", "Previous desktop")}
              disabled={spaces.length < 2}
              onClick={() => moveSpace(-1)}
            >
              <ChevronLeft size={14} />
            </button>
            <button
              type="button"
              className={styles.desktopSpaceTrigger}
              data-testid="desktop-space-trigger"
              aria-haspopup="menu"
              aria-expanded={spacesOpen}
              onClick={() => setSpacesOpen((open) => !open)}
            >
              <span
                className={styles.desktopSpaceDot}
                style={{ background: space?.accent }}
              />
              <span className={styles.desktopSpaceName}>{space?.name}</span>
              <span className={styles.desktopSpaceCount}>
                {space?.apps.length ?? 0}
              </span>
              <ChevronDown size={12} />
            </button>
            <button
              type="button"
              className={styles.desktopSpaceArrow}
              aria-label={t("apps.desktop.nextSpace", "Next desktop")}
              disabled={spaces.length < 2}
              onClick={() => moveSpace(1)}
            >
              <ChevronRight size={14} />
            </button>
            <span className={styles.desktopSpaceDots} aria-hidden>
              {spaces.map((item) => (
                <span
                  key={item.id}
                  className={[
                    styles.desktopSpaceDotItem,
                    item.id === space?.id ? styles.desktopSpaceDotActive : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                />
              ))}
            </span>
          </div>
        )}
        <div className={styles.desktopMenuActions}>
          <span className={styles.desktopHint}>
            {t(
              "apps.desktop.hint",
              "Click an icon to select it and see expert actions; double-click to open.",
            )}
          </span>
          <button
            type="button"
            className={styles.desktopMenuButton}
            data-testid="desktop-mission-button"
            aria-label={t("apps.desktop.mission.button", "Mission Control")}
            onClick={() => {
              setCommandOpen(false);
              setSpacesOpen(false);
              setMissionOpen(true);
            }}
          >
            <LayoutGrid size={13} />
            <span>{t("apps.desktop.mission.button", "Mission Control")}</span>
          </button>
          <button
            type="button"
            className={styles.desktopMenuButton}
            aria-label={t("apps.desktop.command.button", "Search")}
            onClick={toggleCommandPalette}
          >
            <Search size={13} />
            <span>{t("apps.desktop.command.button", "Search")}</span>
          </button>
          <DesktopActivityCenter
            experts={activityExperts}
            windows={activityWindows}
            onOpenExpert={selectSpace}
            onOpenWindow={openNotificationWindow}
          />
          <DesktopNotificationCenter onOpenWindow={openNotificationWindow} />
          <button
            type="button"
            className={styles.desktopMenuButton}
            onClick={toggleFullscreen}
            aria-label={
              fullscreen
                ? t("apps.desktop.exitFullscreen", "Exit full screen")
                : t("apps.desktop.fullscreen", "Full screen")
            }
          >
            {fullscreen ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
            <span>
              {fullscreen
                ? t("apps.desktop.exitFullscreen", "Exit full screen")
                : t("apps.desktop.fullscreen", "Full screen")}
            </span>
          </button>
        </div>
      </div>
      {spacesOpen && (
        <>
          <button
            type="button"
            className={styles.desktopSpacesBackdrop}
            aria-label={t("apps.desktop.closeSpaces", "Close desktop menu")}
            onClick={() => setSpacesOpen(false)}
          />
          <div
            className={styles.desktopSpacesMenu}
            role="menu"
            data-testid="desktop-spaces-menu"
          >
            <div className={styles.desktopSpacesTitle}>
              {t("apps.desktop.spaces", "Expert desktops")}
            </div>
            {spaces.map((item) => (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                className={[
                  styles.desktopSpaceOption,
                  item.id === space?.id ? styles.desktopSpaceOptionActive : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                data-testid="desktop-space-option"
                data-space-id={item.id}
                onClick={() => selectSpace(item.id)}
              >
                <span className={styles.desktopSpaceOptionIcon}>
                  {item.kind === "all" ? (
                    <LayoutGrid size={17} />
                  ) : (
                    <ExpertIcon
                      iconName={item.iconName}
                      iconUrl={item.iconUrl}
                      size={17}
                    />
                  )}
                </span>
                <span className={styles.desktopSpaceOptionText}>
                  <span className={styles.desktopSpaceOptionName}>
                    {item.name}
                  </span>
                  <span className={styles.desktopSpaceOptionMeta}>
                    {fillTemplate(
                      t("apps.desktop.spaceApps", "{{count}} MiniApps"),
                      { count: item.apps.length },
                    )}
                  </span>
                </span>
                <span
                  className={styles.desktopSpaceOptionState}
                  data-state={item.state ?? "unknown"}
                />
                {item.id === space?.id && (
                  <Check size={14} className={styles.desktopSpaceOptionCheck} />
                )}
              </button>
            ))}
          </div>
        </>
      )}
      <div className={styles.desktopIcons}>
        {currentApps.map((app) => (
          <DesktopIcon
            key={app.app_id}
            app={app}
            selected={app.app_id === selectedAppId}
            onSelect={() => setSelectedAppId(app.app_id)}
            onOpen={() => openWindow(app)}
          />
        ))}
      </div>
      {space?.kind === "expert" &&
        space.agentId &&
        currentApps.length === 0 && (
          <div
            className={styles.desktopEmptySpace}
            data-testid="desktop-empty-space"
          >
            <span className={styles.desktopEmptySpaceIcon} aria-hidden>
              <Sparkles size={24} />
            </span>
            <strong>
              {t("apps.desktop.emptySpaceTitle", "This desktop is ready")}
            </strong>
            <p>
              {fillTemplate(
                t(
                  "apps.desktop.emptySpaceBody",
                  "Ask {{name}} to create the first MiniApp for this workspace.",
                ),
                { name: space.name },
              )}
            </p>
            <button
              type="button"
              className={styles.desktopEmptySpaceAction}
              onClick={() => openChatWindow(space.agentId!)}
            >
              <MessageSquare size={14} />
              {fillTemplate(
                t("apps.desktop.emptySpaceCta", "Chat with {{name}}"),
                { name: space.name },
              )}
            </button>
          </div>
        )}
      <div className={styles.desktopWindows}>
        {windows.map((win) => {
          const app =
            win.kind === "app" && win.appId
              ? byId.get(win.appId) ?? null
              : null;
          const winSpace = win.spaceId
            ? spaces.find((item) => item.id === win.spaceId) ?? space
            : win.kind === "chat"
            ? spaces.find((item) => item.id === win.agentId) ?? space
            : space;
          const chatAgent =
            win.kind === "chat" && win.agentId
              ? agents.find((item) => item.agent_id === win.agentId) ?? null
              : null;
          if (win.kind === "app" && !app) return null;
          if (win.kind === "chat" && !win.agentId) return null;
          if (win.kind === "external" && !win.url) return null;
          const title = windowTitles.get(win.id) ?? app?.name ?? "";
          const inSpace = windowSpaceId(win, appSpace) === space?.id;
          const focused = inSpace && win.z === topZ;
          const titleBarVisible = visibleTitleBars.has(win.id);
          return (
            <section
              key={win.id}
              className={[
                styles.desktopWindow,
                focused ? styles.desktopWindowFocused : "",
                win.maximized ? styles.desktopWindowMaximized : "",
                win.minimized ? styles.desktopWindowMinimized : "",
                !inSpace ? styles.desktopWindowHidden : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{
                left: win.x,
                top: win.y,
                width: win.w,
                height: win.h,
                zIndex: win.z,
              }}
              data-testid="desktop-window"
              data-kind={win.kind}
              data-current={inSpace ? "true" : "false"}
              data-app-id={win.appId ?? ""}
              data-agent-id={win.agentId ?? ""}
              data-url={win.url ?? ""}
              data-minimized={win.minimized ? "true" : "false"}
              data-maximized={win.maximized ? "true" : "false"}
              role="group"
              aria-label={title}
              onPointerDown={() => focusWindow(win.id)}
            >
              <span
                className={styles.desktopWindowTopZone}
                data-testid="desktop-window-top-zone"
                aria-hidden
                onPointerEnter={() => revealTitleBar(win.id)}
                onPointerLeave={() => scheduleHideTitleBar(win.id)}
              />
              <header
                className={[
                  styles.desktopWindowTitleBar,
                  titleBarVisible ? styles.desktopWindowTitleBarVisible : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                data-titlebar={titleBarVisible ? "true" : "false"}
                onPointerEnter={() => revealTitleBar(win.id)}
                onPointerLeave={() => scheduleHideTitleBar(win.id)}
                onPointerDown={(event) => startDrag(win, event)}
                onDoubleClick={(event) => {
                  if ((event.target as HTMLElement).closest("button")) return;
                  toggleMaximize(win.id);
                }}
              >
                <span className={styles.desktopTraffic}>
                  <button
                    type="button"
                    className={`${styles.trafficLight} ${styles.trafficClose}`}
                    aria-label={t("apps.desktop.closeWindow", "Close")}
                    onClick={(event) => {
                      event.stopPropagation();
                      closeWindow(win.id);
                    }}
                  />
                  <button
                    type="button"
                    className={`${styles.trafficLight} ${styles.trafficMinimize}`}
                    aria-label={t("apps.desktop.minimizeWindow", "Minimize")}
                    onClick={(event) => {
                      event.stopPropagation();
                      minimizeWindow(win.id);
                    }}
                  />
                  <button
                    type="button"
                    className={`${styles.trafficLight} ${styles.trafficMaximize}`}
                    aria-label={
                      win.maximized
                        ? t("apps.desktop.restoreWindow", "Restore")
                        : t("apps.desktop.maximizeWindow", "Maximize")
                    }
                    onClick={(event) => {
                      event.stopPropagation();
                      toggleMaximize(win.id);
                    }}
                  />
                </span>
                <span className={styles.desktopWindowTitle}>{title}</span>
                <span className={styles.desktopWindowSpacer} />
              </header>
              <div className={styles.desktopWindowBody}>
                {app ? (
                  <MiniAppFrame
                    appId={app.app_id}
                    title={app.name}
                    fill
                    capabilities={app.capabilities}
                    onComposeMessage={(text) => {
                      const target = app.agent_id?.trim() || winSpace?.agentId;
                      if (target) {
                        openChatWindow(target, { app, prompt: text });
                      }
                    }}
                    onOpenExternal={(url) =>
                      openExternalWindow(
                        url,
                        win.spaceId ??
                          winSpace?.id ??
                          space?.id ??
                          UNASSIGNED_SPACE,
                      )
                    }
                  />
                ) : win.kind === "external" && win.url ? (
                  <DesktopWebView
                    url={win.url}
                    title={win.title}
                    onClose={() => closeWindow(win.id)}
                  />
                ) : win.agentId ? (
                  <DesktopChatWindow
                    agentId={win.agentId}
                    agentName={winSpace?.name ?? win.agentId}
                    agent={chatAgent}
                    apps={winSpace?.apps ?? EMPTY_APPS}
                    initialAppId={win.contextAppId}
                    seed={win.seed}
                    onSeedConsumed={() => consumeChatSeed(win.id)}
                    onOpenApp={openWindow}
                    windowId={win.id}
                    active={focused && !win.minimized}
                  />
                ) : null}
              </div>
              {!win.maximized && (
                <span
                  className={styles.desktopWindowResize}
                  aria-hidden
                  onPointerDown={(event) => startResize(win, event)}
                />
              )}
            </section>
          );
        })}
      </div>
      {space?.agentId && (
        <div className={styles.desktopPetArea} data-testid="desktop-pet-area">
          {selectedApp && (
            <div
              className={styles.desktopAppActions}
              data-testid="desktop-app-actions"
              role="group"
              aria-label={t("apps.desktop.appActions", "MiniApp actions")}
            >
              <span className={styles.desktopAppActionsTitle}>
                {selectedApp.name}
              </span>
              {APP_ACTIONS.map(({ key, icon: Icon, labelKey, label }) => (
                <button
                  key={key}
                  type="button"
                  className={styles.desktopAppAction}
                  data-testid={`desktop-action-${key}`}
                  onClick={() => runAction(key, selectedApp, space.agentId!)}
                >
                  <Icon size={14} />
                  <span>{t(labelKey, label)}</span>
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            className={styles.desktopPet}
            data-testid="desktop-pet"
            data-open={chatOpen ? "true" : "false"}
            data-pet-state={petState}
            aria-label={fillTemplate(
              t("apps.desktop.chatWith", "Chat with {{name}}"),
              { name: space.name },
            )}
            onClick={openPetChat}
          >
            <span className={styles.desktopPetBubble}>
              {pendingApproval
                ? fillTemplate(
                    t(
                      "apps.desktop.petNeedsApproval",
                      "{{name}} needs your confirmation",
                    ),
                    { name: space.name },
                  )
                : selectedApp
                ? fillTemplate(
                    t(
                      "apps.desktop.petBubbleSelected",
                      "Let's work on {{name}}",
                    ),
                    { name: selectedApp.name },
                  )
                : fillTemplate(
                    t("apps.desktop.petBubble", "Click to chat with {{name}}"),
                    { name: space.name },
                  )}
            </span>
            <span
              className={styles.desktopPetBody}
              style={{ "--pet-accent": space.accent } as CSSProperties}
              aria-hidden
            >
              <span className={styles.desktopPetEarLeft} />
              <span className={styles.desktopPetEarRight} />
              <span className={styles.desktopPetFace}>
                <span className={styles.desktopPetEyes}>
                  <span className={styles.desktopPetEye} />
                  <span className={styles.desktopPetEye} />
                </span>
                <span className={styles.desktopPetMouth} />
              </span>
              <span className={styles.desktopPetBadge}>
                <ExpertIcon
                  iconName={space.iconName}
                  iconUrl={space.iconUrl}
                  size={15}
                />
              </span>
              <span
                className={styles.desktopPetStatus}
                data-state={space.state ?? "unknown"}
              />
            </span>
            <span className={styles.desktopPetName}>{space.name}</span>
          </button>
        </div>
      )}
      <DesktopDock items={dockItems} />
      <DesktopCommandPalette
        open={commandOpen}
        onClose={() => setCommandOpen(false)}
        items={commandItems}
      />
      <DesktopMissionControl
        open={missionOpen}
        spaces={missionSpaces}
        onClose={() => setMissionOpen(false)}
        onSelectSpace={(id) => {
          selectSpace(id);
          setMissionOpen(false);
        }}
        onFocusWindow={(id) => {
          openNotificationWindow(id);
          setMissionOpen(false);
        }}
      />
      {booting && (
        <DesktopBootScreen
          expertName={
            bootSpace?.name ?? t("apps.desktop.boot.fallbackExpert", "Agent OS")
          }
          expertColor={bootSpace?.accent}
          iconName={bootSpace?.iconName}
          iconUrl={bootSpace?.iconUrl}
          onDone={finishBoot}
        />
      )}
    </div>
  );
}
