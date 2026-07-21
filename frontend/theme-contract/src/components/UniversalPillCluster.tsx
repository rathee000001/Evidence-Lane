import { useLayoutEffect, useRef, type HTMLAttributes, type ReactNode } from "react";

export const UNIVERSAL_PILL_CONTENT_UTILIZATION = 0.9;
const MEASURING_SELECTOR = '[data-pill-measuring="true"]';

export function useUniversalPillCluster(clusterId?: string) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const root = ref.current;
    if (!root || !clusterId) return undefined;
    let measuring = false;

    const clusterPills = () => Array.from(root.querySelectorAll<HTMLElement>(".universal-pill")).filter(
      (pill) => pill.closest<HTMLElement>("[data-universal-pill-cluster]") === root
        && pill.dataset.pillClusterExempt !== "true",
    );
    const measure = () => {
      if (measuring) return;
      measuring = true;
      const pills = clusterPills();
      pills.forEach((pill) => {
        pill.dataset.pillMeasuring = "true";
        // Inline important measurement state defeats older, more-specific
        // command-bar rules so the measurement cannot feed back from flexed
        // layout width into the universal content-fit authority.
        pill.style.setProperty("flex", "0 0 auto", "important");
        pill.style.setProperty("width", "max-content", "important");
        pill.style.setProperty("min-width", "max-content", "important");
      });
      const measurements = pills.map((pill) => {
        const content = pill.querySelector<HTMLElement>(".universal-pill__content");
        if (!content) return { minimum: 0, preferred: 0 };
        const contentStyle = window.getComputedStyle(content);
        const children = Array.from(content.children).filter((child): child is HTMLElement => child instanceof HTMLElement && window.getComputedStyle(child).display !== "none");
        const gap = Number.parseFloat(contentStyle.columnGap || contentStyle.gap || "0") || 0;
        // A DOM Range measures the rendered contents rather than the flexed
        // label box. That prevents the prior cluster width from feeding back
        // into the next content-fit measurement.
        const contentWidth = children.reduce((total, child) => {
          const range = document.createRange();
          range.selectNodeContents(child);
          const rangeWidth = range.getBoundingClientRect().width;
          const childStyle = window.getComputedStyle(child);
          const chrome = (Number.parseFloat(childStyle.paddingLeft) || 0)
            + (Number.parseFloat(childStyle.paddingRight) || 0)
            + (Number.parseFloat(childStyle.borderLeftWidth) || 0)
            + (Number.parseFloat(childStyle.borderRightWidth) || 0);
          return total + rangeWidth + chrome;
        }, 0) + Math.max(0, children.length - 1) * gap;
        const pillStyle = window.getComputedStyle(pill);
        const pillInlineChrome = (Number.parseFloat(pillStyle.paddingLeft) || 0)
          + (Number.parseFloat(pillStyle.paddingRight) || 0)
          + (Number.parseFloat(pillStyle.borderLeftWidth) || 0)
          + (Number.parseFloat(pillStyle.borderRightWidth) || 0);
        return {
          minimum: contentWidth + pillInlineChrome,
          preferred: contentWidth / UNIVERSAL_PILL_CONTENT_UTILIZATION + pillInlineChrome,
        };
      });
      const minimumWidth = Math.max(0, ...measurements.map(({ minimum }) => minimum));
      const preferredWidth = Math.max(0, ...measurements.map(({ preferred }) => preferred));
      const containedGrid = root.querySelector<HTMLElement>('[data-universal-pill-grid="contained"]');
      const containedGridStyle = containedGrid ? window.getComputedStyle(containedGrid) : null;
      const containedTrackWidths = containedGrid
        ? (containedGridStyle?.gridTemplateColumns.match(/\d+(?:\.\d+)?px/g) ?? [])
          .map((value) => Number.parseFloat(value))
          .filter((value) => Number.isFinite(value) && value > 0)
        : [];
      const containedGridPadding = containedGridStyle
        ? (Number.parseFloat(containedGridStyle.paddingLeft) || 0)
          + (Number.parseFloat(containedGridStyle.paddingRight) || 0)
        : 0;
      const containedGridGap = Number.parseFloat(containedGridStyle?.columnGap || "0") || 0;
      const containedTrackWidth = containedGrid && containedTrackWidths.length > 0
        ? (containedGrid.clientWidth
          - containedGridPadding
          - Math.max(0, containedTrackWidths.length - 1) * containedGridGap) / containedTrackWidths.length
        : Number.POSITIVE_INFINITY;
      const clusterStyle = window.getComputedStyle(root);
      const clusterGap = Number.parseFloat(clusterStyle.columnGap || clusterStyle.gap || "0") || 0;
      const distributedPromptBar = clusterId === "prompt-bar";
      const directPillChildren = Array.from(root.children).filter((child): child is HTMLElement => (
        child instanceof HTMLElement
        && child.classList.contains("universal-pill")
        && window.getComputedStyle(child).display !== "none"
      ));
      const exemptPillWidth = directPillChildren
        .filter((pill) => pill.dataset.pillClusterExempt === "true")
        .reduce((total, pill) => total + pill.getBoundingClientRect().width, 0);
      const rootInnerWidth = root.clientWidth
        - (Number.parseFloat(clusterStyle.paddingLeft) || 0)
        - (Number.parseFloat(clusterStyle.paddingRight) || 0);
      const parent = root.parentElement;
      const parentStyle = parent ? window.getComputedStyle(parent) : null;
      const availableWidth = parent && parentStyle
        ? parent.clientWidth
          - (Number.parseFloat(parentStyle.paddingLeft) || 0)
          - (Number.parseFloat(parentStyle.paddingRight) || 0)
        : Number.POSITIVE_INFINITY;
      const firstTop = pills[0]?.getBoundingClientRect().top ?? 0;
      const isSingleRow = pills.length <= 1 || pills.every(
        (pill) => Math.abs(pill.getBoundingClientRect().top - firstTop) < 2,
      );
      root.dataset.pillClusterLayout = isSingleRow ? "row" : "stack";
      const distributedWidth = rootInnerWidth
        - exemptPillWidth
        - Math.max(0, directPillChildren.length - 1) * clusterGap;
      const fittingWidth = isSingleRow && pills.length > 0
        ? (distributedPromptBar ? distributedWidth : availableWidth - Math.max(0, pills.length - 1) * clusterGap) / pills.length
        : preferredWidth;
      const unconstrainedTargetWidth = distributedPromptBar && Number.isFinite(fittingWidth) && fittingWidth > 0
        ? fittingWidth
        : Number.isFinite(fittingWidth) && fittingWidth > 0
        ? Math.max(minimumWidth, Math.min(preferredWidth, fittingWidth))
        : preferredWidth;
      const targetWidth = Number.isFinite(containedTrackWidth)
        ? containedTrackWidth
        : unconstrainedTargetWidth;
      if (targetWidth > 0) {
        const widthPixels = Number.isFinite(containedTrackWidth) ? Math.floor(targetWidth) : Math.ceil(targetWidth);
        const width = `${widthPixels}px`;
        if (root.style.getPropertyValue("--universal-pill-cluster-width") !== width) {
          root.style.setProperty("--universal-pill-cluster-width", width);
          root.style.setProperty("--universal-pill-cluster-half-width", `${widthPixels / 2}px`);
        }
        pills.forEach((pill) => {
          delete pill.dataset.pillMeasuring;
          pill.style.setProperty("flex", `0 0 ${width}`, "important");
          pill.style.setProperty("width", width, "important");
          pill.style.setProperty("min-width", width, "important");
        });
      } else {
        pills.forEach((pill) => {
          delete pill.dataset.pillMeasuring;
          pill.style.removeProperty("flex");
          pill.style.removeProperty("width");
          pill.style.removeProperty("min-width");
        });
      }
      measuring = false;
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    if (root.parentElement) observer.observe(root.parentElement);
    const containedGrid = root.querySelector<HTMLElement>('[data-universal-pill-grid="contained"]');
    if (containedGrid) observer.observe(containedGrid);
    clusterPills().forEach((pill) => observer.observe(pill));
    window.addEventListener("resize", measure);
    void document.fonts?.ready.then(measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [clusterId]);

  return ref;
}

export type UniversalPillClusterProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
  clusterId: string;
};

export function UniversalPillCluster({ children, clusterId, className = "", ...props }: UniversalPillClusterProps) {
  const ref = useUniversalPillCluster(clusterId);
  return (
    <div {...props} ref={ref} className={className} data-universal-pill-cluster={clusterId} data-measuring-selector={MEASURING_SELECTOR}>
      {children}
    </div>
  );
}
