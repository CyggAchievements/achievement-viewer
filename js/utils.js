// Get GitHub username from repository URL
export function getGitHubUserInfo() {
  const currentUrl = window.location.href;
  let githubUsername = 'User';
  let githubAvatarUrl = 'https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png';
  let githubRepo = 'achievement-viewer';

  const repoMatch = currentUrl.match(/github\.io\/([^\/]+)/);
  if (repoMatch) {
    githubUsername = currentUrl.split('.github.io')[0].split('//')[1];
    githubAvatarUrl = `https://github.com/${githubUsername.toLowerCase()}.png`;
    githubRepo = repoMatch[1];
  }

  return { username: githubUsername, avatarUrl: githubAvatarUrl, repo: githubRepo };
}

// Percentage calculation
export function calculatePercentage(unlocked, total) {
    return total > 0 ? Math.round((unlocked / total) * 100) : 0;
}

// Date formatting
export function formatUnlockDate(timestamp) {
    return new Date(timestamp * 1000).toLocaleDateString();
}

// ==========================================
// SHARED CACHING & FETCHING LOGIC
// ==========================================

const CACHE_KEY = 'forks-cache-v1';
const CACHE_TTL = 30 * 60 * 1000; // 30 minutes

// Shared fetch helper
export const fetchJSON = (url) => fetch(url).then((r) => (r.ok ? r.json() : null));

// Shared repo detection (for Hub/Forks logic)
export function detectRepo() {
  const host = location.hostname;
  const path = location.pathname.split('/').filter(Boolean);
  if (!host.endsWith('.github.io') || path.length === 0) return { owner: 'Roschach96', repo: 'achievement-viewer' };
  return { owner: host.replace('.github.io', ''), repo: path[0] };
}

// Shared root repo resolution
export async function resolveRootRepo(owner, repo) {
  const info = await fetchJSON(`https://api.github.com/repos/${owner}/${repo}`);
  if (!info) throw new Error('Repo not found');

  if (info.fork && info.parent) {
    return { owner: info.parent.owner.login, repo: info.parent.name };
  }
  // Return the actual cased login from the API
  return { owner: info.owner.login, repo: info.name };
}

// Shared fork fetching with CACHE
export async function fetchAllForks(owner, repo) {
  const cached = localStorage.getItem(CACHE_KEY);
  if (cached) {
    try {
      const { time, data } = JSON.parse(cached);
      // If cache is fresh (less than 10 mins old), use it!
      if (Date.now() - time < CACHE_TTL && data) return data;
    } catch (e) {
      // ignore parse errors
    }
  }
  
  // Cache expired or missing? Fetch fresh data.
  let forks = [];
  let page = 1;
  while (true) {
    const data = await fetchJSON(`https://api.github.com/repos/${owner}/${repo}/forks?per_page=100&page=${page}`);
    if (!data || !data.length) break;
    forks.push(...data);
    page++;
  }
  
  // Save to cache
  localStorage.setItem(CACHE_KEY, JSON.stringify({ time: Date.now(), data: forks }));
  return forks;
}
