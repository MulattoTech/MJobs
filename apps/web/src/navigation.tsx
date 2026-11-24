export type Tab = 'Jobs'|'Pipeline'|'Profile'|'Career Stories'|'Learning'|'Sources'|'Applications'|'Review Queue'|'AI Settings'|'Activity';

export const MAIN_TABS: Tab[] = [
  'Jobs',
  'Pipeline',
  'Profile',
  'Career Stories',
  'Learning',
  'Sources',
  'Applications',
  'Review Queue',
  'AI Settings',
  'Activity',
];

export function tabTitle(tab: Tab): string {
  if(tab==='Jobs')return 'Find your next opportunity.';
  if(tab==='Pipeline')return 'Move opportunities forward.';
  if(tab==='Profile')return 'Your experience, in your words.';
  if(tab==='Career Stories')return 'Build reusable career stories.';
  if(tab==='Learning')return 'Understand your matches.';
  if(tab==='Sources')return 'Choose where to look.';
  if(tab==='Applications')return 'Every application, accounted for.';
  if(tab==='Review Queue')return 'Review application blockers.';
  if(tab==='AI Settings')return 'Choose models by task.';
  return 'See what is happening.';
}

export type FirstRunNavigationTarget = {
  tab: Tab;
  anchor: string;
};

const FIRST_RUN_NAVIGATION: Record<string, FirstRunNavigationTarget> = {
  'create-profile':{tab:'Profile',anchor:'create-profile'},
  'upload-resume':{tab:'Profile',anchor:'upload-resume'},
  'review-extracted-facts':{tab:'Profile',anchor:'review-extracted-facts'},
  'configure-desired-jobs':{tab:'Sources',anchor:'configure-desired-jobs'},
  'configure-location':{tab:'Profile',anchor:'configure-location'},
  'configure-application-preferences':{tab:'Profile',anchor:'configure-application-preferences'},
  'configure-ai':{tab:'AI Settings',anchor:''},
  'enable-sources':{tab:'Sources',anchor:'enable-sources'},
};

export function firstRunNavigationTarget(stepId: string): FirstRunNavigationTarget | undefined {
  return FIRST_RUN_NAVIGATION[stepId];
}

type MainNavigationProps = {
  tab: Tab;
  jobsCount: number;
  applicationsCount: number;
  reviewQueueCount: number;
  onNavigate: (tab: Tab) => void;
};

export function MainNavigation({
  tab,
  jobsCount,
  applicationsCount,
  reviewQueueCount,
  onNavigate,
}: MainNavigationProps) {
  return <aside className="sidebar">
    <a className="brand" href="/">M<span>Jobs</span><small>YOUR LOCAL WORKSPACE</small></a>
    <nav aria-label="Main navigation">
      {MAIN_TABS.map(item=><button key={item} aria-current={tab===item?'page':undefined} onClick={()=>onNavigate(item)}>
        {item}
        <span>{item==='Jobs'?jobsCount:item==='Applications'?applicationsCount:item==='Review Queue'?reviewQueueCount:''}</span>
      </button>)}
    </nav>
    <div className="side-note">
      <strong>Built around your facts.</strong>
      <p>Discover opportunities, prepare truthful documents, and keep a clear record.</p>
      <span className="status">External submission OFF</span>
    </div>
  </aside>;
}
