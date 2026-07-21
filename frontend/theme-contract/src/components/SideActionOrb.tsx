import { GlassIconOrb } from "./GlassIconOrb";

export type SideCommandAction = "add" | "search" | "pin";
export type SideAction = SideCommandAction | "expand" | "collapse";

const paths: Record<SideAction, React.ReactNode> = {
  add: <><path d="M12 5v14M5 12h14" /></>,
  search: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></>,
  pin: <><path d="m9 4 6 0-1 5 3 3v2H7v-2l3-3-1-5ZM12 14v7" /></>,
  expand: <path d="m9 6 6 6-6 6" />,
  collapse: <path d="m15 6-6 6 6 6" />,
};

export function SideActionOrb({ action, size = 47 }: { action: SideAction; size?: number }) {
  return <GlassIconOrb className="side-action-orb" color="#69d9f5" size={size} label={action}><svg viewBox="0 0 24 24" aria-hidden="true">{paths[action]}</svg></GlassIconOrb>;
}
