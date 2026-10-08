import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DocumentArchive, type ArchivableDocument } from './document-archive';

function findButtons(node:unknown):React.ReactElement<React.ButtonHTMLAttributes<HTMLButtonElement>>[] {
  if(!React.isValidElement(node))return [];
  const found=node.type==='button'?[node as React.ReactElement<React.ButtonHTMLAttributes<HTMLButtonElement>>]:[];
  const children=(node.props as {children?:React.ReactNode}).children;
  return [...found,...React.Children.toArray(children).flatMap(findButtons)];
}

const active:ArchivableDocument={
  id:'document-1',profileId:'profile-1',applicationId:'application-1',revisionId:'revision-1',
  label:'Platform resume',kind:'resume',archivedAt:null,
};
const archived:ArchivableDocument={
  id:'document-2',profileId:'profile-1',applicationId:'application-2',revisionId:'revision-2',
  label:'Cover letter version 2',kind:'cover letter',archivedAt:'2026-10-07T12:00:00Z',
};

describe('document archive',()=>{
  it('shows active and archived history states and offers clearly named review actions',()=>{
    const html=renderToStaticMarkup(<DocumentArchive documents={[active,archived]} onArchive={vi.fn()} onRestore={vi.fn()}/>);
    expect(html).toContain('Active');
    expect(html).toContain('available in active documents');
    expect(html).toContain('Archived');
    expect(html).toContain('archived ');
    expect(html).toContain('Archive after review');
    expect(html).toContain('Restore after review');
    expect(html).toContain('application application-1');
    expect(html).toContain('revision revision-1');
    expect(html).toContain('aria-label="Archive resume Platform resume for profile profile-1"');
    expect(html).toContain('aria-label="Restore cover letter Cover letter version 2 for profile profile-1"');
  });

  it('emits only the selected row, preserving profile, application, and revision scope',()=>{
    const onArchive=vi.fn();const onRestore=vi.fn();
    const tree=DocumentArchive({documents:[active,archived],onArchive,onRestore});
    const buttons=findButtons(tree);
    expect(buttons).toHaveLength(2);
    buttons[0]!.props.onClick?.({} as React.MouseEvent<HTMLButtonElement>);
    buttons[1]!.props.onClick?.({} as React.MouseEvent<HTMLButtonElement>);
    expect(onArchive).toHaveBeenCalledExactlyOnceWith(active);
    expect(onRestore).toHaveBeenCalledExactlyOnceWith(archived);
  });

  it('disables archive and restore while a parent action is pending',()=>{
    const html=renderToStaticMarkup(<DocumentArchive documents={[active,archived]} busy onArchive={vi.fn()} onRestore={vi.fn()}/>);
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });
});
