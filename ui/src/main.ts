import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import App from "./App.vue";
import "./style.css";

const router = createRouter({
  history: createWebHistory("/ui/"),
  routes: [
    {
      path: "/",
      redirect: "/jobs",
    },
    {
      path: "/jobs",
      component: () => import("./components/JobList.vue"),
    },
    {
      path: "/jobs/:id",
      component: () => import("./components/JobDetail.vue"),
    },
    {
      path: "/episodes",
      component: () => import("./components/JobList.vue"),
    },
    {
      path: "/episodes/:id",
      component: () => import("./components/EpisodeDetail.vue"),
    },
    {
      path: "/lora-adapters",
      component: () => import("./components/LoraAdapterManager.vue"),
    },
    {
      path: "/lora-training",
      component: () => import("./components/LoraTrainingWorkbench.vue"),
    },
  ],
});

createApp(App).use(router).mount("#app");
