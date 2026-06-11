export const selectPopupProps = {
  listHeight: 220,
  popupMatchSelectWidth: false,
  getPopupContainer: (triggerNode: HTMLElement) => triggerNode.parentElement || document.body
};
