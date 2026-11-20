import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { JobSalary, type JobSalaryData } from './job-salary';

// Exact canonical scalar shape returned by the connected fixture API.
const actual = {salary_min:80000, salary_max:110000, salary_currency:'USD',
  salary_period:'YEAR', salary_source:'SOURCE_PROVIDED'};
const legacy = {salary:[{min:70000,max:95000,currency:'EUR',period:'YEAR'}],
  salary_source:'SOURCE_PROVIDED'};
const render = (job: unknown) => renderToStaticMarkup(<JobSalary job={job as JobSalaryData}/>);

describe('job salary detail', () => {
  it('renders actual canonical source salary values without requiring a legacy array', () => {
    const html = render({...actual, attributes:{...actual}});
    expect(html).toContain('Source-reported salary range (not independently verified)');
    expect(html).toContain('USD 80,000–110,000 per year');
    expect(html).not.toContain('Estimated');
  });

  it('uses attribute scalars when top-level scalar fields are absent', () => {
    expect(render({attributes:actual})).toContain('USD 80,000–110,000 per year');
  });

  it('keeps canonical scalar values ahead of stale attributes and legacy values', () => {
    const html = render({...actual, attributes:{...legacy, salary_min:70000,salary_max:95000}});
    expect(html).toContain('USD 80,000–110,000 per year');
    expect(html).not.toContain('95,000');
    expect(html).not.toContain('EUR');
  });

  it('preserves legacy arrays and their estimated provenance', () => {
    expect(render({attributes:legacy})).toContain('EUR 70,000–95,000 per year');
    const html = render({attributes:{...legacy,salary_source:'ESTIMATED'}});
    expect(html).toContain('Estimated salary range (not employer-confirmed)');
    expect(html).not.toContain('Source-reported');
  });

  it.each(['SOURCE_PROVIDED','EMPLOYER_PROVIDED'])(
    'retains the source-reported disclosure for %s', source => {
      expect(render({...actual,salary_source:source})).toContain(
        'Source-reported salary range (not independently verified)');
    });

  it('never upgrades explicit estimates or missing provenance to reported salary', () => {
    expect(render({...actual,salary_source:'ESTIMATED'})).toContain(
      'Estimated salary range (not employer-confirmed)');
    const html = render({...actual,salary_source:null});
    expect(html).toContain('Recorded salary range (provenance not specified)');
    expect(html).not.toContain('Source-reported');
  });

  it('shows hourly values without converting them to annual salary', () => {
    const html = render({...actual,salary_min:40.5,salary_max:55.75,salary_currency:'CAD',salary_period:'HOUR'});
    expect(html).toContain('CAD 40.5–55.75 per hour');
    expect(html).not.toContain('per year');
  });

  it.each([
    [{salary_min:80000,salary_max:null},'USD from 80,000 per year'],
    [{salary_min:null,salary_max:110000},'USD up to 110,000 per year'],
    [{salary_min:80000,salary_max:80000},'USD 80,000 per year'],
    [{salary_min:0,salary_max:0},'USD 0 per year'],
  ])('does not invent a missing bound for %j', (bounds, expected) => {
    expect(render({...actual,...bounds})).toContain(expected);
  });

  it('does not infer currency or period from conflicting attribute metadata', () => {
    const html = render({salary_min:10,salary_max:20,attributes:actual});
    expect(html).toContain('Currency not specified: 10–20 (period not specified)');
    expect(html).not.toContain('USD');
    expect(html).not.toContain('per year');
  });

  it.each([{}, {attributes:{}}, {salary_min:null,salary_max:null},
    {attributes:{salary:[]}}, {attributes:{salary:'untrusted'}}])(
    'does not invent salary when unavailable: %j', job => expect(render(job)).toBe(''));

  it.each([
    {salary_min:'80000'}, {salary_min:true}, {salary_min:-1},
    {salary_min:NaN}, {salary_max:Infinity}, {salary_min:120000,salary_max:110000},
    {salary_min:null,salary_max:null},
  ])('does not rescue missing or malformed canonical values from a stale legacy array: %j', invalid => {
    expect(render({...actual,...invalid,attributes:legacy})).toBe('');
  });

  it('filters malformed legacy entries without interpreting raw markup', () => {
    const html = render({attributes:{...legacy,salary:[
      {min:'90000',max:100000,currency:'USD',period:'YEAR'},
      {min:70000,max:95000,currency:'<script>bad()</script>',period:'<img src=x>'},
    ]}});
    expect(html).not.toContain('100,000');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('<img');
    expect(html).toContain('Currency not specified: 70,000–95,000 (period not specified)');
  });
});
