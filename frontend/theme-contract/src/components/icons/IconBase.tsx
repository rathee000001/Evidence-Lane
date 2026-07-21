export type EvidenceToolIconProps = {
  size?: number | string;
  color?: string;
  className?: string;
  title?: string;
};

type IconBaseProps = EvidenceToolIconProps & { svg: string; name: string };

export function IconBase({ svg, name, size = 32, color = "currentColor", className = "", title }: IconBaseProps) {
  const paths = svg.replace(/^<svg[^>]*>/, "").replace(/<\/svg>\s*$/, "");
  return (
    <svg
      className={`evidence-tool-icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 128 128"
      preserveAspectRatio="xMidYMid meet"
      style={{ color }}
      role="img"
      aria-label={title ?? name}
      dangerouslySetInnerHTML={{ __html: paths }}
    />
  );
}
