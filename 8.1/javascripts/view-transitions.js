(() => {
  let replayingClick = false;

  window.addEventListener(
    "click",
    (event) => {
      const link = event.target.closest("a[href]");
      if (
        replayingClick ||
        !link ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        link.target ||
        link.hasAttribute("download") ||
        typeof document.startViewTransition !== "function" ||
        window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ) {
        return;
      }

      const destination = new URL(link.href, window.location.href);
      if (destination.origin !== window.location.origin) {
        return;
      }

      event.preventDefault();
      event.stopImmediatePropagation();

      document.startViewTransition(() => {
        if (
          destination.pathname === window.location.pathname &&
          destination.search === window.location.search
        ) {
          const target = destination.hash
            ? document.querySelector(destination.hash)
            : document.documentElement;
          target?.scrollIntoView({ behavior: "instant", block: "start" });
          history.pushState(null, "", destination);
          return;
        }

        return new Promise((resolve) => {
          let awaitingNavigation = false;
          let subscription;
          const timeout = window.setTimeout(() => {
            subscription?.unsubscribe();
            resolve();
          }, 5000);

          if (typeof document$ !== "undefined") {
            subscription = document$.subscribe(() => {
              if (!awaitingNavigation) {
                return;
              }
              window.clearTimeout(timeout);
              subscription.unsubscribe();
              resolve();
            });
          }

          awaitingNavigation = true;
          replayingClick = true;
          link.click();
          replayingClick = false;

          if (!subscription) {
            window.location.assign(destination);
          }
        });
      });
    },
    true,
  );
})();
