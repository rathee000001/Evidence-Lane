import edgeNodeButtonUrl from "./edge-node-button.png";
import folderNodeButtonUrl from "./folder-node-button.png";
import rootNodeLiveUrl from "./root-node-live.png";
import universalGlassOrbUrl from "./universal-glass-orb.png";

export const T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1 = Object.freeze({
  runtimeLaw: "LIGHTWEIGHT_TRANSPARENT_RASTER_INSIDE_PROCEDURAL_3D_GLASS",
  rootButtonLaw: "ONE_COMPLETE_CLEAR_GLASS_ORB_CONTAINING_LIVE_ROOT_NODE",
  topologyOrbLaw: "ONLY_OUTERMOST_TOPOLOGY_ORB_HAS_SELECTED_BRAIN_NEON_RIM",
  focusTravelLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NO_ORB_POP",
  connectorLineCount: 0,
  glbUse: "REFERENCE_ONLY_MEASURED_TOO_HEAVY_FOR_RUNTIME",
  assets: Object.freeze({
    rootNode: Object.freeze({
      url: rootNodeLiveUrl,
      sha256: "C8569E264EB69D1BB4D642E30F60799E5690D63DAF6652F3EC2CE6DBA6FA7264",
      role: "ROOT_NODE_INSIDE_CLEAR_GLASS_BUTTON",
    }),
    folderNode: Object.freeze({
      url: folderNodeButtonUrl,
      sha256: "A424A7880712DBB5FE8000EA82496D04E57BAF716A7F2F0D9A75D8970B1F5C24",
      role: "FOLDER_NODE_BUTTON",
    }),
    edgeNode: Object.freeze({
      url: edgeNodeButtonUrl,
      sha256: "D6C372367220D27116DAC7B42DC93BFF0F6B9E8E2F797F7D401C1429A4AA76A5",
      role: "FILE_EDGE_BUTTON",
    }),
    universalGlassOrb: Object.freeze({
      url: universalGlassOrbUrl,
      sha256: "0EFF64A4DA09F28D662F889A7A08A53EEE2F3BB9B146195A811569EC1FCBED2E",
      role: "OUTERMOST_TOPOLOGY_GLASS_REFERENCE_LAYER",
    }),
  }),
});
