import { t023CharacterProfileManifest } from "../contracts/T023UiManifests";

export type ExecutiveScannerProps = {
  size?: number | string;
  className?: string;
  combinedCharacterUrl?: string;
};

export function ExecutiveScanner({
  size = 440,
  className = "",
  combinedCharacterUrl = t023CharacterProfileManifest.temporaryCombinedCharacterAsset,
}: ExecutiveScannerProps) {
  return (
    <div
      className={`executive-scanner ${className}`.trim()}
      style={{ width: size, height: size }}
      role="img"
      aria-label="Evidence Lane combined character at the premium-lens Toolchain anchor"
      data-renderer-state={t023CharacterProfileManifest.rendererState}
      data-visual-presentation="combined-png-public-v1"
    >
      <span className="executive-scanner__image-stack" aria-hidden="true">
        <img
          className="executive-scanner__combined-character"
          src={combinedCharacterUrl}
          alt=""
          draggable={false}
        />
      </span>
      <span className="executive-scanner__node-proof" data-character-node="body" aria-hidden="true" />
      <span className="executive-scanner__node-proof" data-character-node="face" aria-hidden="true" />
    </div>
  );
}
